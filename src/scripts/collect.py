"""
collect.py
Interroge le flux GTFS-RT TripUpdates de TBM toutes les X secondes
et enregistre/actualise les observations dans la base SQLite.
Conçu pour tourner en continu, en tâche de fond, sur plusieurs semaines.
"""

import sqlite3
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

PROJECT_ROOT = Path(__file__).resolve().parents[2]
URL_TRIPUPDATES = (
    "https://bdx.mecatran.com/utw/ws/gtfsfeed/realtime/bordeaux"
    "?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt"
)
DB_PATH = str(PROJECT_ROOT / "data" / "vigie_tbm.db")
POLL_INTERVAL_SECONDS = 60
GAP_THRESHOLD_SECONDS = 180  # 3x l'intervalle normal de 60s, marge de sécurité


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(PROJECT_ROOT / "data" / "collect.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def fetch_feed() -> gtfs_realtime_pb2.FeedMessage:
    response = requests.get(URL_TRIPUPDATES, timeout=15)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed


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


def log_trip_status(conn, trip_id, start_date, route_id, trip_schedule_relationship, feed_timestamp):
    conn.execute("""
        INSERT INTO trip_status (trip_id, start_date, route_id, schedule_relationship, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(trip_id, start_date) DO UPDATE SET
            schedule_relationship = excluded.schedule_relationship,
            last_seen_at = excluded.last_seen_at
    """, (trip_id, start_date, route_id, trip_schedule_relationship, feed_timestamp))


def process_feed(conn, feed: gtfs_realtime_pb2.FeedMessage) -> int:
    feed_timestamp = feed.header.timestamp
    n_rows = 0
    

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        start_date = tu.trip.start_date
        route_id = tu.trip.route_id
        direction_id = tu.trip.direction_id
        trip_schedule_relationship = gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name(
            tu.trip.schedule_relationship
        )

        log_trip_status(conn, trip_id, start_date, route_id, trip_schedule_relationship, feed_timestamp)

        for stu in tu.stop_time_update:
            schedule_relationship = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(
                stu.schedule_relationship
            )

            # Le premier arrêt d'un trajet a souvent une arrival.delay
            # incohérente (bus garé avant l'heure de service) -> on l'ignore.
            arrival_delay = None
            if stu.HasField("arrival") and stu.stop_sequence != 1:
                arrival_delay = stu.arrival.delay

            departure_delay = None
            departure_time = None
            if stu.HasField("departure"):
                departure_delay = stu.departure.delay
                departure_time = stu.departure.time  # epoch absolu GTFS-RT

            upsert_observation(
                conn, trip_id, start_date, route_id, direction_id,
                stu.stop_sequence, stu.stop_id, schedule_relationship,
                arrival_delay, departure_delay, departure_time, feed_timestamp,
            )
            n_rows += 1

    conn.commit()
    return n_rows


def record_gap_if_any(conn, last_success_ts, now):
    if last_success_ts is not None and (now - last_success_ts) > GAP_THRESHOLD_SECONDS:
        conn.execute(
            "INSERT INTO collection_gaps (gap_start, gap_end) VALUES (?, ?)",
            (int(last_success_ts), int(now)),
        )
        conn.commit()
        logger.warning("Trou de collecte détecté : %.0f minutes", (now - last_success_ts) / 60)


def get_last_known_success(conn):
    row = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()
    return float(row[0]) if row[0] is not None else None


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")    
    logger.info("Démarrage de la collecte Vigie TBM (intervalle: %ss)", POLL_INTERVAL_SECONDS)

    last_success_ts = get_last_known_success(conn)

    while True:
        cycle_start = time.monotonic()
        try:
            feed = fetch_feed()

            now = time.time()
            record_gap_if_any(conn, last_success_ts, now)
            last_success_ts = now

            n_rows = process_feed(conn, feed)
            logger.info(
                "OK - %d entités, %d observations mises à jour (feed ts=%s)",
                len(feed.entity), n_rows, feed.header.timestamp,
            )
        except requests.RequestException as e:
            logger.warning("Échec de récupération du flux : %s", e)
        except Exception as e:
            logger.exception("Erreur inattendue pendant le traitement : %s", e)

        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0, POLL_INTERVAL_SECONDS - elapsed))

if __name__ == "__main__":
    main()