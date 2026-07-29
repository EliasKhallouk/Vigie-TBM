"""
gtfs_static.py
Télécharge le GTFS statique TBM et charge les tables routes/stops
dans la base SQLite, pour pouvoir traduire route_id/stop_id
en noms lisibles (nom de ligne, nom d'arrêt).
route_type : 2 = tram, 3 = bus selon la norme GTFS
"""

import sqlite3
import zipfile
import io
import csv
import requests

GTFS_STATIC_URL = (
    "https://bdx.mecatran.com/utw/ws/gtfsfeed/static/bordeaux"
    "?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt"
)
DB_PATH = "data/vigie_tbm.db"


def download_gtfs_zip(url: str) -> zipfile.ZipFile:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(response.content))


def create_static_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS routes (
            route_id TEXT PRIMARY KEY,
            route_short_name TEXT,
            route_long_name TEXT,
            route_type INTEGER
        );

        CREATE TABLE IF NOT EXISTS stops (
            stop_id TEXT PRIMARY KEY,
            stop_name TEXT,
            stop_lat REAL,
            stop_lon REAL
        );
    """)
    conn.commit()


def load_routes(conn: sqlite3.Connection, gtfs_zip: zipfile.ZipFile) -> int:
    with gtfs_zip.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        rows = [
            (row["route_id"], row.get("route_short_name"),
             row.get("route_long_name"), row.get("route_type"))
            for row in reader
        ]

    conn.executemany("""
        INSERT INTO routes (route_id, route_short_name, route_long_name, route_type)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(route_id) DO UPDATE SET
            route_short_name = excluded.route_short_name,
            route_long_name = excluded.route_long_name,
            route_type = excluded.route_type
    """, rows)
    conn.commit()
    return len(rows)


def load_stops(conn: sqlite3.Connection, gtfs_zip: zipfile.ZipFile) -> int:
    with gtfs_zip.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        rows = [
            (row["stop_id"], row.get("stop_name"),
             row.get("stop_lat"), row.get("stop_lon"))
            for row in reader
        ]

    conn.executemany("""
        INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(stop_id) DO UPDATE SET
            stop_name = excluded.stop_name,
            stop_lat = excluded.stop_lat,
            stop_lon = excluded.stop_lon
    """, rows)
    conn.commit()
    return len(rows)


def main():
    conn = sqlite3.connect(DB_PATH)
    create_static_tables(conn)

    gtfs_zip = download_gtfs_zip(GTFS_STATIC_URL)
    print("Fichiers disponibles dans le GTFS :", gtfs_zip.namelist())

    n_routes = load_routes(conn, gtfs_zip)
    n_stops = load_stops(conn, gtfs_zip)
    print(f"{n_routes} lignes chargées, {n_stops} arrêts chargés.")

    conn.close()


if __name__ == "__main__":
    main()