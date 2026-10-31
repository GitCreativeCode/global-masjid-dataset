import os
import sqlite3
import gzip
import json
import time
import math
import urllib.request
import re
import duckdb

# Schema matching Simple Athan App SQLite engine
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mosques (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    street TEXT,
    city TEXT,
    postcode TEXT,
    phone TEXT,
    website TEXT,
    last_updated INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mosques_lat_lon ON mosques (latitude, longitude);
"""

# Country-to-Region mapping
REGIONS = {
    "north_america": ["CA", "US", "MX", "BM", "GL"],
    "europe": ["GB", "FR", "DE", "IT", "ES", "NL", "BE", "CH", "AT", "SE", "NO", "DK", "FI", "IE", "PL", "PT", "GR", "CZ", "RO", "HU", "UA", "RU", "BA", "AL", "XK", "TR"],
    "asia": ["SA", "AE", "QA", "KW", "OM", "BH", "JO", "LB", "SY", "IQ", "IR", "PK", "IN", "BD", "ID", "MY", "SG", "TH", "PH", "AF", "UZ", "KZ", "KG", "TJ", "TM", "YE"],
    "africa": ["EG", "MA", "DZ", "TN", "LY", "SD", "NG", "KE", "ZA", "SN", "GH", "CI", "ML", "NE", "TD", "SO", "ET", "TZ", "UG"],
    "south_america": ["BR", "AR", "CO", "PE", "CL", "VE", "EC", "BO", "PY", "UY", "GY", "SR"],
    "central_america": ["GT", "BZ", "SV", "HN", "NI", "CR", "PA", "CU", "DO", "PR", "JM", "TT"],
    "australia_oceania": ["AU", "NZ", "FJ", "PG"],
    "antarctica": ["AQ"]
}

OUTPUT_DIR = "dist"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000  # meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def rank_candidate(r):
    """
    Ranks candidates when multiple listings exist at the same address or footprint:
    1. Direct sanctuary names (Masjid, Mosque, Musalla, Jamia) receive highest priority (+20).
    2. Facility / community center names receive secondary priority (+10).
    3. Short acronyms (<= 4 chars like 'SKC') are penalized (-15).
    4. Verified contact info (phone/website) adds confidence (+2 each).
    """
    name = (r[1] or '').lower()
    score = 0
    if any(k in name for k in ['masjid', 'mosque', 'musalla', 'musallah', 'jamia', 'jame ']):
        score += 20
    elif any(k in name for k in ['center', 'centre', 'community']):
        score += 10
    
    if len((r[1] or '').strip()) <= 4:
        score -= 15
        
    if r[7]: score += 2  # Has phone
    if r[8]: score += 2  # Has website
    score += min(len(r[1] or ''), 30) * 0.1
    return score

def deduplicate_places(rows):
    """
    Spatial clustering deduplication:
    Merges duplicate points sharing the same address or within 35 meters,
    selecting the most accurate mosque/sanctuary representation.
    """
    print(f"Deduplicating {len(rows):,} raw records...")
    start_t = time.time()
    
    # Bucket by rough 0.01-degree grid (~1km) for fast spatial lookup
    grid = {}
    for idx, r in enumerate(rows):
        lat, lon = r[2], r[3]
        cell = (round(lat, 2), round(lon, 2))
        grid.setdefault(cell, []).append((idx, r))
    
    assigned = set()
    deduped = []
    
    for idx, r1 in enumerate(rows):
        if idx in assigned:
            continue
        
        assigned.add(idx)
        cluster = [r1]
        lat1, lon1 = r1[2], r1[3]
        street1 = (r1[4] or '').strip().lower()
        city1 = (r1[5] or '').strip().lower()
        
        # Check neighboring 3x3 grid cells
        cell_lat, cell_lon = round(lat1, 2), round(lon1, 2)
        candidates = []
        for dlat in (-0.01, 0.0, 0.01):
            for dlon in (-0.01, 0.0, 0.01):
                c = (round(cell_lat + dlat, 2), round(cell_lon + dlon, 2))
                if c in grid:
                    candidates.extend(grid[c])
        
        for other_idx, r2 in candidates:
            if other_idx in assigned or other_idx == idx:
                continue
            
            lat2, lon2 = r2[2], r2[3]
            street2 = (r2[4] or '').strip().lower()
            city2 = (r2[5] or '').strip().lower()
            
            same_address = (street1 and street2 and street1 == street2 and city1 == city2)
            dist = haversine(lat1, lon1, lat2, lon2)
            
            # Within 35m or identical street address
            if same_address or dist < 35:
                cluster.append(r2)
                assigned.add(other_idx)
        
        if len(cluster) == 1:
            deduped.append(cluster[0])
        else:
            cluster.sort(key=rank_candidate, reverse=True)
            deduped.append(cluster[0])
            
    print(f"Deduplication complete in {time.time() - start_t:.2f}s: Reduced from {len(rows):,} to {len(deduped):,} places (-{len(rows) - len(deduped):,} duplicates).")
    return deduped

def get_latest_overture_version():
    try:
        req = urllib.request.Request("https://stac.overturemaps.org/catalog.json", headers={"User-Agent": "GlobalMasjidCompiler/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            links = data.get("links", [])
            for l in links:
                if l.get("rel") == "child" and l.get("latest") is True:
                    match = re.search(r'(\d{4}-\d{2}-\d{2}\.\d+)', l.get("href", ""))
                    if match:
                        return match.group(1)
            for l in reversed(links):
                if l.get("rel") == "child":
                    match = re.search(r'(\d{4}-\d{2}-\d{2}\.\d+)', l.get("href", ""))
                    if match:
                        return match.group(1)
    except Exception as e:
        print(f"Warning: Could not fetch latest release dynamically ({e}). Defaulting to 2026-10-21.0")
    return "2026-10-21.0"

def main():
    print("=" * 60)
    print("  Global Mosque Dataset Compiler (Production Edition)")
    print("=" * 60)
    
    release_ver = get_latest_overture_version()
    print(f"Targeting Overture Release: {release_ver}")
    
    con = duckdb.connect()
    print("Loading DuckDB S3 & Spatial extensions...")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region = 'us-west-2';")
    
    now_ts = int(time.time())
    
    # Principled semantic query without hardcoded individual mosque names
    query = f"""
    SELECT 
        id,
        names.primary AS name,
        ROUND(ST_Y(geometry), 7) AS latitude,
        ROUND(ST_X(geometry), 7) AS longitude,
        addresses[1].freeform AS street,
        addresses[1].locality AS city,
        addresses[1].postcode AS postcode,
        phones[1] AS phone,
        websites[1] AS website,
        UPPER(COALESCE(addresses[1].country, '')) AS country
    FROM read_parquet(
        's3://overturemaps-us-west-2/release/{release_ver}/theme=places/type=place/*',
        filename = true,
        hive_partitioning = 1
    )
    WHERE (
        -- 1. Standard Overture religious taxonomy & categories
        taxonomy.primary ILIKE '%muslim%' 
        OR basic_category ILIKE '%muslim%'
        OR taxonomy.primary ILIKE '%mosque%' 
        OR basic_category ILIKE '%mosque%'
        
        -- 2. Primary prayer hall names across all Islamic traditions
        OR names.primary ILIKE '%mosque%' 
        OR names.primary ILIKE '%masjid%'
        OR names.primary ILIKE '%musalla%'
        OR names.primary ILIKE '%musallah%'
        OR names.primary ILIKE '%jama masjid%'
        OR names.primary ILIKE '%jamia masjid%'
        OR names.primary ILIKE '%jame masjid%'
        OR names.primary ILIKE '%markaz%'
        OR names.primary ILIKE '%baitul%'
        OR names.primary ILIKE '%imambargah%'
        OR names.primary ILIKE '%husainia%'
        OR names.primary ILIKE '%hussainia%'
        OR names.primary ILIKE '%prayer hall%'
        
        -- 3. Islamic Community Centres (multi-functional places of worship)
        OR names.primary ILIKE '%islamic cent%'
        OR names.primary ILIKE '%islamic comm%'
        OR names.primary ILIKE '%muslim cent%'
        OR names.primary ILIKE '%muslim comm%'
    )
    -- Exclude non-religious commercial categories
    AND basic_category NOT IN (
        'restaurant', 'cafe', 'food_and_beverage_store', 'grocery_store',
        'fashion_and_apparel_store', 'specialty_store', 'hardware_home_and_garden_store',
        'electronics_store', 'books_music_and_video_store',
        'real_estate_service', 'financial_service', 'attorney_or_law_firm',
        'medical_service', 'dental_clinic', 'automotive_service',
        'sport_or_fitness_facility', 'gym',
        'elementary_school', 'high_school', 'preschool', 'school'
    )
    -- Exclude non-mosque entities (administrative offices, educational schools, commercial services)
    AND names.primary NOT ILIKE '%elementary school%'
    AND names.primary NOT ILIKE '%high school%'
    AND names.primary NOT ILIKE '%academy%'
    AND names.primary NOT ILIKE '%halal certification%'
    AND names.primary NOT ILIKE '%restaurant%'
    AND names.primary NOT ILIKE '%grocery%'
    AND names.primary NOT ILIKE '%meat%'
    AND names.primary NOT ILIKE '%halal food%'
    AND names.primary NOT ILIKE '%catering%'
    AND names.primary NOT ILIKE '%boutique%'
    AND names.primary NOT ILIKE '%travel%'
    AND names.primary NOT ILIKE '%tours%'
    AND names.primary NOT ILIKE '%mosqueter%'
    AND names.primary NOT ILIKE '%mosqueda%'
    AND names.primary NOT ILIKE '%farmacia%'
    AND names.primary IS NOT NULL
    AND ST_Y(geometry) IS NOT NULL 
    AND ST_X(geometry) IS NOT NULL;
    """
    
    print("Fetching and filtering mosques across the globe from S3...")
    start_time = time.time()
    raw_rows = con.execute(query).fetchall()
    print(f"Extraction complete in {time.time() - start_time:.2f}s! Raw places found: {len(raw_rows):,}")
    
    # Apply automated spatial clustering deduplication
    rows = deduplicate_places(raw_rows)
    
    # In-memory regional buckets
    regional_data = {region: [] for region in REGIONS}
    regional_data["other"] = []
    
    for r in rows:
        c_code = r[9] or ""
        matched = False
        for region, countries in REGIONS.items():
            if c_code in countries:
                regional_data[region].append(r[:9] + (now_ts,))
                matched = True
                break
        if not matched:
            regional_data["other"].append(r[:9] + (now_ts,))

    # Compile regional databases
    metadata = {"generated_at": now_ts, "regions": {}}
    global_db_path = os.path.join(OUTPUT_DIR, "mosques_global.db")
    
    # 1. Create global database
    if os.path.exists(global_db_path):
        os.remove(global_db_path)
    conn_global = sqlite3.connect(global_db_path)
    conn_global.executescript(CREATE_TABLE_SQL)
    
    all_records = []
    for reg, records in regional_data.items():
        all_records.extend(records)
        
        # Save regional DB
        reg_db_path = os.path.join(OUTPUT_DIR, f"mosques_{reg}.db")
        if os.path.exists(reg_db_path):
            os.remove(reg_db_path)
        conn_reg = sqlite3.connect(reg_db_path)
        conn_reg.executescript(CREATE_TABLE_SQL)
        conn_reg.executemany("INSERT OR REPLACE INTO mosques VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
        conn_reg.commit()
        conn_reg.close()
        
        # Compress regional DB
        gz_path = f"{reg_db_path}.gz"
        with open(reg_db_path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=9) as f_out:
            f_out.writelines(f_in)
        
        metadata["regions"][reg] = {
            "count": len(records),
            "raw_size_mb": round(os.path.getsize(reg_db_path) / (1024 * 1024), 2),
            "gz_size_mb": round(os.path.getsize(gz_path) / (1024 * 1024), 2),
        }
        print(f" - [{reg.upper()}] {len(records):,} mosques -> {metadata['regions'][reg]['gz_size_mb']} MB (gzipped)")
    
    # Write all to global
    conn_global.executemany("INSERT OR REPLACE INTO mosques VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", all_records)
    conn_global.commit()
    conn_global.close()
    
    global_gz_path = f"{global_db_path}.gz"
    with open(global_db_path, "rb") as f_in, gzip.open(global_gz_path, "wb", compresslevel=9) as f_out:
        f_out.writelines(f_in)
        
    metadata["global"] = {
        "count": len(all_records),
        "raw_size_mb": round(os.path.getsize(global_db_path) / (1024 * 1024), 2),
        "gz_size_mb": round(os.path.getsize(global_gz_path) / (1024 * 1024), 2),
    }
    print(f"\n[GLOBAL MERGED] {len(all_records):,} mosques -> {metadata['global']['gz_size_mb']} MB (gzipped)")
    
    # Write metadata
    with open(os.path.join(OUTPUT_DIR, "version.json"), "w") as f:
        json.dump(metadata, f, indent=2)
    print("\nDataset generation completed successfully!")

if __name__ == "__main__":
    main()