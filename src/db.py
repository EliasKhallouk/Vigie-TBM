import sqlite3

conn = sqlite3.connect("vigie_tbm.db")
conn.execute("""
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
)

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
)
""")
conn.commit()

def upsert_observation(conn, trip_id, start_date, route_id, direction_id,
                        stop_sequence, stop_id, schedule_relationship,
                        arrival_delay, departure_delay, departure_time,
                        feed_timestamp):
    conn.execute("""
        INSERT INTO observations
            (trip_id, start_date, route_id, direction_id, stop_sequence,
             stop_id, schedule_relationship, arrival_delay, departure_delay,
             departure_time, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trip_id, start_date, stop_sequence) DO UPDATE SET
            schedule_relationship = excluded.schedule_relationship,
            arrival_delay = excluded.arrival_delay,
            departure_delay = excluded.departure_delay,
            departure_time = excluded.departure_time,
            last_seen_at = excluded.last_seen_at
    """, (trip_id, start_date, route_id, direction_id, stop_sequence,
          stop_id, schedule_relationship, arrival_delay, departure_delay,
          departure_time, feed_timestamp))


columns = {
    row[1]
    for row in conn.execute("PRAGMA table_info(observations)")
}
if "departure_time" not in columns:
    conn.execute("ALTER TABLE observations ADD COLUMN departure_time INTEGER")