"""
collect_alerts.py
Interroge le flux GTFS-RT ServiceAlerts de TBM toutes les X secondes
et enregistre/actualise les alertes dans la base SQLite.
Concu pour tourner en continu, en tâche de fond, sur plusieurs semaines.
Le flux ServiceAlerts ne contient pas de stop_id : on ne peut pas lier
une alerte à un arrêt specifique de maniere automatique.
"""

import sqlite3
import time
import logging
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

PROJECT_ROOT = Path(__file__).resolve().parents[2]
URL_SERVICEALERTS = (
    "https://bdx.mecatran.com/utw/ws/gtfsfeed/alerts/bordeaux"
    "?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt"
)
DB_PATH = str(PROJECT_ROOT / "data" / "vigie_tbm.db")
POLL_INTERVAL_SECONDS = 120


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(PROJECT_ROOT / "data" / "alerts.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS service_alerts (
            alert_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            active_period_start INTEGER NOT NULL,
            active_period_end INTEGER,
            header_text TEXT,
            description_text TEXT,
            cause INTEGER,
            last_seen_at INTEGER NOT NULL,
            PRIMARY KEY (alert_id, route_id, active_period_start)
        )
    """)
    conn.commit()


def fetch_feed() -> gtfs_realtime_pb2.FeedMessage:
    response = requests.get(URL_SERVICEALERTS, timeout=15)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed


def _get_text(translated_string) -> str | None:
    if not translated_string or not translated_string.translation:
        return None
    for t in translated_string.translation:
        if t.language and t.language.startswith("fr"):
            return t.text
    return translated_string.translation[0].text


def process_feed(conn, feed: gtfs_realtime_pb2.FeedMessage) -> int:
    feed_timestamp = feed.header.timestamp
    n_alerts = 0

    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue

        alert = entity.alert
        alert_id = entity.id
        header_text = _get_text(alert.header_text)
        description_text = _get_text(alert.description_text)
        cause = alert.cause

        periods = alert.active_period
        if not periods:
            periods = [gtfs_realtime_pb2.Alert.TimeRange()]  # single unbounded period

        for period in periods:
            period_start = period.start if period.HasField("start") else 0
            period_end = period.end if period.HasField("end") else None

            route_ids = [
                ie.route_id for ie in alert.informed_entity if ie.route_id
            ]
            if not route_ids:
                route_ids = [""]

            for route_id in route_ids:
                conn.execute("""
                    INSERT INTO service_alerts
                        (alert_id, route_id, active_period_start, active_period_end,
                         header_text, description_text, cause, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(alert_id, route_id, active_period_start) DO UPDATE SET
                        active_period_end = excluded.active_period_end,
                        header_text = excluded.header_text,
                        description_text = excluded.description_text,
                        cause = excluded.cause,
                        last_seen_at = excluded.last_seen_at
                """, (alert_id, route_id, period_start, period_end,
                      header_text, description_text, cause, feed_timestamp))
                n_alerts += 1

    conn.commit()
    return n_alerts


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    init_db(conn)

    logger.info("Demarrage de la collecte des alertes (intervalle: %ss)", POLL_INTERVAL_SECONDS)

    while True:
        cycle_start = time.monotonic()
        try:
            feed = fetch_feed()
            n_alerts = process_feed(conn, feed)
            logger.info(
                "OK - %d entites, %d alertes mises a jour (feed ts=%s)",
                len(feed.entity), n_alerts, feed.header.timestamp,
            )
        except requests.RequestException as e:
            logger.warning("Echec de recuperation du flux d'alertes : %s", e)
        except Exception as e:
            logger.exception("Erreur inattendue pendant le traitement des alertes : %s", e)

        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0, POLL_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
