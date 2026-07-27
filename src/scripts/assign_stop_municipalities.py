#!/usr/bin/env python3
"""Rattache géographiquement chaque arrêt TBM à une commune de la Métropole.

La classification repose sur une jointure spatiale entre les coordonnées des
arrêts du GTFS et les contours officiels publiés par Bordeaux Métropole. Aucun
nom d'arrêt, code postal ou géocodage textuel n'est utilisé : les arrêts situés
hors des 28 contours restent explicitement non attribués et sont exportés pour
contrôle.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "vigie_tbm.db"
DEFAULT_BOUNDARIES_URL = (
    "https://opendata.bordeaux-metropole.fr/api/explore/v2.1/catalog/datasets/"
    "fv_commu_s/exports/geojson?lang=fr&timezone=Europe%2FParis"
)
REVERSE_ADDRESS_URL = "https://api-adresse.data.gouv.fr/reverse/?"


def point_on_segment(point: tuple[float, float], start: list[float], end: list[float]) -> bool:
    """Return whether a lon/lat point lies on a polygon segment (within tolerance)."""
    x, y = point
    x1, y1 = start[:2]
    x2, y2 = end[:2]
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > 1e-10:
        return False
    return min(x1, x2) - 1e-10 <= x <= max(x1, x2) + 1e-10 and min(y1, y2) - 1e-10 <= y <= max(y1, y2) + 1e-10


def point_in_ring(point: tuple[float, float], ring: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. Points on a boundary count as inside."""
    x, y = point
    inside = False
    for index, current in enumerate(ring):
        previous = ring[index - 1]
        if point_on_segment(point, previous, current):
            return True
        x1, y1 = previous[:2]
        x2, y2 = current[:2]
        crosses_ray = (y1 > y) != (y2 > y)
        if crosses_ray and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def point_in_polygon(point: tuple[float, float], polygon: list[list[list[float]]]) -> bool:
    """A point is inside the outer ring and outside all interior holes."""
    if not polygon or not point_in_ring(point, polygon[0]):
        return False
    return not any(point_in_ring(point, hole) for hole in polygon[1:])


def geometry_contains(point: tuple[float, float], geometry: dict) -> bool:
    coordinates = geometry["coordinates"]
    polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
    return any(point_in_polygon(point, polygon) for polygon in polygons)


def bounding_box(geometry: dict) -> tuple[float, float, float, float]:
    coordinates = geometry["coordinates"]
    polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
    points = [point for polygon in polygons for ring in polygon for point in ring]
    longitudes, latitudes = zip(*((point[0], point[1]) for point in points))
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def load_boundaries(source_file: Path | None, source_url: str) -> tuple[dict, str]:
    if source_file:
        return json.loads(source_file.read_text(encoding="utf-8")), str(source_file.resolve())
    with urlopen(source_url, timeout=60) as response:  # nosec B310 - official, configurable public data URL
        return json.load(response), source_url


def reverse_geocode_commune(latitude: float, longitude: float) -> tuple[str, str] | None:
    """Resolve the few stops outside Bordeaux Métropole through the national API Adresse.

    This is only a fallback after the official point-in-polygon join. The city
    code returned by the API is an INSEE code, which makes the result auditable.
    """
    url = REVERSE_ADDRESS_URL + urlencode({"lat": latitude, "lon": longitude})
    request = Request(url, headers={"User-Agent": "Vigie-TBM/1.0 (stop municipality assignment)"})
    try:
        with urlopen(request, timeout=20) as response:  # nosec B310 - official public API
            payload = json.load(response)
    except OSError:
        return None
    features = payload.get("features", [])
    if not features:
        return None
    properties = features[0].get("properties", {})
    city_code, city = properties.get("citycode"), properties.get("city")
    return (str(city_code), city) if city_code and city else None


def initialize_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS municipalities (
            insee_code TEXT PRIMARY KEY,
            commune_name TEXT NOT NULL,
            boundary_source TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stop_municipalities (
            stop_id TEXT PRIMARY KEY,
            insee_code TEXT NOT NULL,
            commune_name TEXT NOT NULL,
            assignment_method TEXT NOT NULL,
            assigned_at INTEGER NOT NULL,
            FOREIGN KEY (stop_id) REFERENCES stops(stop_id),
            FOREIGN KEY (insee_code) REFERENCES municipalities(insee_code)
        );

        CREATE INDEX IF NOT EXISTS idx_stop_municipalities_commune
            ON stop_municipalities(commune_name);
        """
    )


def assign_stops(
    conn: sqlite3.Connection,
    boundaries: dict,
    source: str,
    resolve_outside: bool,
) -> list[tuple[str, str, float, float]]:
    """Refresh municipalities and return the stops not covered by a boundary."""
    now = int(time.time())
    prepared = []
    for feature in boundaries.get("features", []):
        properties = feature.get("properties", {})
        geometry = feature.get("geometry")
        if not geometry or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        prepared.append({
            "insee": str(properties["insee"]),
            "name": properties["nom"],
            "geometry": geometry,
            "bbox": bounding_box(geometry),
        })
    if not prepared:
        raise ValueError("Aucun polygone communal exploitable n'a été trouvé dans le GeoJSON.")
    official_names = {item["insee"]: item["name"] for item in prepared}

    conn.execute("DELETE FROM municipalities")
    conn.executemany(
        "INSERT INTO municipalities (insee_code, commune_name, boundary_source, updated_at) VALUES (?, ?, ?, ?)",
        [(item["insee"], item["name"], source, now) for item in prepared],
    )
    conn.execute("DELETE FROM stop_municipalities")

    assignments = []
    unassigned = []
    fallback_municipalities: dict[str, str] = {}
    for stop_id, stop_name, latitude, longitude in conn.execute(
        "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops ORDER BY stop_id"
    ):
        point = (float(longitude), float(latitude))
        match = None
        for municipality in prepared:
            west, south, east, north = municipality["bbox"]
            if west <= point[0] <= east and south <= point[1] <= north and geometry_contains(point, municipality["geometry"]):
                match = municipality
                break
        if match:
            assignments.append((stop_id, match["insee"], match["name"], "point-in-polygon", now))
        else:
            fallback = reverse_geocode_commune(float(latitude), float(longitude)) if resolve_outside else None
            if fallback:
                insee_code, commune_name = fallback
                # Prefer the official Bordeaux Métropole spelling when the API
                # resolves a point just outside a polygon edge of a member city.
                commune_name = official_names.get(insee_code, commune_name)
                fallback_municipalities[insee_code] = commune_name
                assignments.append((stop_id, insee_code, commune_name, "api-adresse-reverse", now))
            else:
                unassigned.append((stop_id, stop_name, latitude, longitude))
            # The API is only called for the small outside-metropole remainder.
            if resolve_outside:
                time.sleep(0.05)
    conn.executemany(
        """INSERT INTO municipalities (insee_code, commune_name, boundary_source, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(insee_code) DO UPDATE SET commune_name = excluded.commune_name,
                                                boundary_source = excluded.boundary_source,
                                                updated_at = excluded.updated_at""",
        [(code, name, "https://api-adresse.data.gouv.fr/", now) for code, name in fallback_municipalities.items()],
    )
    conn.executemany(
        """INSERT INTO stop_municipalities
           (stop_id, insee_code, commune_name, assignment_method, assigned_at)
           VALUES (?, ?, ?, ?, ?)""",
        assignments,
    )
    conn.commit()
    return unassigned


def export_unassigned(rows: list[tuple[str, str, float, float]], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as file_handle:
        writer = csv.writer(file_handle)
        writer.writerow(["stop_id", "stop_name", "stop_lat", "stop_lon", "reason"])
        writer.writerows([(*row, "commune_non_resolue") for row in rows])


def main() -> int:
    parser = argparse.ArgumentParser(description="Rattache les arrêts TBM aux communes de Bordeaux Métropole.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB, help="Base SQLite contenant stops.")
    parser.add_argument("--boundaries-file", type=Path, help="GeoJSON de contours communaux, pour un fonctionnement hors ligne.")
    parser.add_argument("--boundaries-url", default=DEFAULT_BOUNDARIES_URL, help="URL du GeoJSON officiel.")
    parser.add_argument("--no-reverse-fallback", action="store_true", help="N'interroge pas l'API Adresse pour les arrêts hors Bordeaux Métropole.")
    parser.add_argument("--unassigned-csv", type=Path, default=PROJECT_ROOT / "reports" / "output" / "stops_outside_bordeaux_metropole.csv", help="Export des arrêts sans rattachement.")
    args = parser.parse_args()
    if not args.db_path.exists():
        parser.error(f"Base introuvable : {args.db_path}")
    try:
        boundaries, source = load_boundaries(args.boundaries_file, args.boundaries_url)
        with sqlite3.connect(args.db_path) as conn:
            initialize_tables(conn)
            unassigned = assign_stops(conn, boundaries, source, resolve_outside=not args.no_reverse_fallback)
            assigned = conn.execute("SELECT COUNT(*) FROM stop_municipalities").fetchone()[0]
            counts = conn.execute(
                "SELECT commune_name, COUNT(*) FROM stop_municipalities GROUP BY commune_name ORDER BY commune_name"
            ).fetchall()
        export_unassigned(unassigned, args.unassigned_csv)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Erreur : {error}")
        return 1
    print(f"{assigned} arrêts rattachés à une commune ; {len(unassigned)} arrêts sans commune résolue.")
    for commune, count in counts:
        print(f"  - {commune} : {count} arrêts")
    print(f"Contrôle des arrêts non attribués : {args.unassigned_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
