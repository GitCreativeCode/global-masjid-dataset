import os
import sqlite3
import gzip
import json
import time
import urllib.request
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

def get_latest_overture_version():
    try:
        req = urllib.request.Request("https://stac.overturemaps.org/catalog.json", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("latest", "2026-10-21.0")
    except Exception as e:
        print(f"Warning: Could not fetch STAC catalog ({e}), using fallback version.")
        return "2026-10-21.0"

def main():
    print("=" * 60)
    print("  Global Masjid Dataset Compiler")
    print("=" * 60)
    
    release_version = get_latest_overture_version()
    print(f"Targeting Overture Release: {release_version}")
    
    con = duckdb.connect()
    print("Loading DuckDB S3 & Spatial extensions...")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region = 'us-west-2';")
    
    now_ts = int(time.time())
    s3_path = f"s3://overturemaps-us-west-2/release/{release_version}/theme=places/type=place/*"
    
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
        UPPER(country) AS country
    FROM read_parquet(
        '{s3_path}',
        filename = true,
        hive_partitioning = 1
    )
    WHERE (
        taxonomy.primary ILIKE '%mosque%' 
        OR basic_category ILIKE '%mosque%' 
        OR names.primary ILIKE '%mosque%' 
        OR names.primary ILIKE '%masjid%'
        OR names.primary ILIKE '%jama masjid%'
        OR names.primary ILIKE '%musalla%'
    )
    AND names.primary IS NOT NULL
    AND ST_Y(geometry) IS NOT NULL 
    AND ST_X(geometry) IS NOT NULL;
    """
    
    print("Fetching and filtering mosques across the globe from S3...")
    start_time = time.time()
    rows = con.execute(query).fetchall()
    print(f"Extraction complete in {time.time() - start_time:.2f}s! Total mosques found: {len(rows):,}")
    
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
    metadata = {"generated_at": now_ts, "version": release_version, "regions": {}}
    global_db_path = os.path.join(OUTPUT_DIR, "mosques_global.db")
    
    if os.path.exists(global_db_path):
        os.remove(global_db_path)
    conn_global = sqlite3.connect(global_db_path)
    conn_global.executescript(CREATE_TABLE_SQL)
    
    all_records = []
    for reg, records in regional_data.items():
        all_records.extend(records)
        
        reg_db_path = os.path.join(OUTPUT_DIR, f"mosques_{reg}.db")
        if os.path.exists(reg_db_path):
            os.remove(reg_db_path)
        conn_reg = sqlite3.connect(reg_db_path)
        conn_reg.executescript(CREATE_TABLE_SQL)
        conn_reg.executemany("INSERT OR REPLACE INTO mosques VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
        conn_reg.commit()
        conn_reg.close()
        
        gz_path = f"{reg_db_path}.gz"
        with open(reg_db_path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=9) as f_out:
            f_out.writelines(f_in)
        
        metadata["regions"][reg] = {
            "count": len(records),
            "raw_size_mb": round(os.path.getsize(reg_db_path) / (1024 * 1024), 2),
            "gz_size_mb": round(os.path.getsize(gz_path) / (1024 * 1024), 2),
        }
        print(f" - [{reg.upper()}] {len(records):,} mosques -> {metadata['regions'][reg]['gz_size_mb']} MB (gzipped)")
    
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
    
    with open(os.path.join(OUTPUT_DIR, "version.json"), "w") as f:
        json.dump(metadata, f, indent=2)
    print("\nDataset generation completed successfully!")

if __name__ == "__main__":
    main()
