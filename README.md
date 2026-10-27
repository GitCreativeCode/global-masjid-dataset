# Global Mosque Dataset (Masjid DB)

An open-source, tracker-free SQLite dataset of mosques and Islamic places of worship worldwide, derived from **OpenStreetMap** and the **Overture Maps Foundation**.

## Features
* **100% Offline**: Pre-compiled SQLite databases (`.db.gz`) with spatial B-Tree indexes.
* **Complete Metadata**: Includes Mosque Name, Coordinates, Street Address, City, Postal Code, Phone Number, and Website where available.
* **Regional & Global**: Download the whole world (~12 MB) or just your continent (~1 MB).
* **Automatic Updates**: Synced on the 1st of every month via GitHub Actions.

## Downloads
Download the latest compressed databases from [Releases](https://github.com/GitCreativeCode/global-masjid-dataset/releases/tag/latest):
* `mosques_global.db.gz` (Entire World)
* `mosques_north_america.db.gz` (Canada, USA, Mexico)
* `mosques_europe.db.gz` (UK, France, Germany, Balkans, etc.)
* `mosques_asia.db.gz` (Middle East, South Asia, SE Asia, etc.)
* `mosques_africa.db.gz` (North & Sub-Saharan Africa)
* `version.json` (Record counts and release metadata)

## Schema
```sql
CREATE TABLE mosques (
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
CREATE INDEX idx_mosques_lat_lon ON mosques (latitude, longitude);
```

## License
Open Database License (ODbL) & CDLA-Permissive v2.0. Attribution to OpenStreetMap & Overture Maps contributors.