import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "vigie_tbm.db"
conn = sqlite3.connect(DB_PATH)
conn.executescript("""
CREATE TABLE IF NOT EXISTS observations (
    trip_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    route_id TEXT NOT NULL,
    direction_id INTEGER,
    stop_sequence INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    schedule_relationship TEXT,
    arrival_delay INTEGER,
    departure_delay INTEGER,
    departure_time INTEGER,
    last_seen_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, start_date, stop_sequence)
);

CREATE TABLE IF NOT EXISTS daily_line_stats (
    stat_date TEXT NOT NULL,
    route_id TEXT NOT NULL,
    route_short_name TEXT,
    n_observations INTEGER,
    retard_moyen_s REAL,
    retard_median_s REAL,
    pct_retard_5min REAL,
    pct_avance_1min REAL,
    n_arrets_sautes INTEGER,
    computed_at INTEGER NOT NULL,
    PRIMARY KEY (stat_date, route_id)
);

CREATE TABLE IF NOT EXISTS collection_gaps (
    gap_start INTEGER NOT NULL,
    gap_end INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_status (
    trip_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    route_id TEXT NOT NULL,
    schedule_relationship TEXT NOT NULL,
    last_seen_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, start_date)
)
""")
conn.commit()


columns = {
    row[1]
    for row in conn.execute("PRAGMA table_info(observations)")
}
if "departure_time" not in columns:
    conn.execute("ALTER TABLE observations ADD COLUMN departure_time INTEGER")