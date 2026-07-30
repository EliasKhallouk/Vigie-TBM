"""
analyze.py
Calcule les métriques de ponctualité par ligne à partir des observations
collectées. Ne considère que les observations "terminées" (le trajet
est sorti du flux temps réel, donc le retard enregistré est définitif).
"""

import logging
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "vigie_tbm.db"
FRESHNESS_BUFFER_SECONDS = 20 * 60  # 20 minutes


def load_completed_observations(conn):
    max_ts = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    if max_ts is None:
        return pd.DataFrame()
    cutoff = max_ts - FRESHNESS_BUFFER_SECONDS

    now = time.time()
    min_plausible = int(datetime(2020, 1, 1).timestamp())
    gaps = conn.execute(
        "SELECT gap_start, gap_end FROM collection_gaps "
        "WHERE gap_start >= ? AND gap_end <= ? AND gap_end > gap_start",
        (min_plausible, int(now) + 3600),
    ).fetchall()

    query = """
        SELECT o.*, r.route_short_name, s.stop_name
        FROM observations o
        LEFT JOIN routes r ON o.route_id = r.route_id
        LEFT JOIN stops s ON o.stop_id = s.stop_id
        WHERE o.last_seen_at < ?
    """
    df = pd.read_sql(query, conn, params=(cutoff,))

    for gap_start, gap_end in gaps:
        suspect_window_start = gap_start - FRESHNESS_BUFFER_SECONDS
        in_window = df["last_seen_at"].between(suspect_window_start, gap_end)
        n_excluded = in_window.sum()
        if n_excluded:
            logger = logging.getLogger(__name__)
            logger.info("Trou de collecte exclu : %d observations (fenêtre de %.0f min)",
                        n_excluded, (gap_end - suspect_window_start) / 60)
            df = df[~in_window]

    return df


def compute_line_stats(df: pd.DataFrame) -> pd.DataFrame:
    # On exclut les arrêts sautés (SKIPPED) du calcul de retard,
    # mais on garde leur compte à part (c'est une info différente : une annulation, pas un retard)
    completed = df[df["schedule_relationship"] == "SCHEDULED"].dropna(subset=["departure_delay"])

    stats = completed.groupby(["route_id", "route_short_name"]).agg(
        n_observations=("departure_delay", "count"),
        retard_moyen_s=("departure_delay", "mean"),
        retard_median_s=("departure_delay", "median"),
        pct_retard_5min=("departure_delay", lambda x: (x > 300).mean() * 100),
        pct_avance_1min=("departure_delay", lambda x: (x < -60).mean() * 100),
    ).reset_index()

    skipped_counts = (
        df[df["schedule_relationship"] == "SKIPPED"]
        .groupby(["route_id", "route_short_name"])
        .size()
        .rename("n_arrets_sautes")
    )

    stats = stats.merge(skipped_counts, on=["route_id", "route_short_name"], how="left")
    stats["n_arrets_sautes"] = stats["n_arrets_sautes"].fillna(0).astype(int)

    return stats.sort_values("pct_retard_5min", ascending=False)

def save_stats_to_db(conn: sqlite3.Connection, stats: pd.DataFrame, stat_date: str) -> None:
    conn.execute("""
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

    computed_at = int(time.time())
    rows = [
        (stat_date, r.route_id, r.route_short_name, r.n_observations,
         r.retard_moyen_s, r.retard_median_s, r.pct_retard_5min,
         r.pct_avance_1min, r.n_arrets_sautes, computed_at)
        for r in stats.itertuples()
    ]

    conn.executemany("""
        INSERT INTO daily_line_stats
            (stat_date, route_id, route_short_name, n_observations,
             retard_moyen_s, retard_median_s, pct_retard_5min,
             pct_avance_1min, n_arrets_sautes, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stat_date, route_id) DO UPDATE SET
            route_short_name = excluded.route_short_name,
            n_observations = excluded.n_observations,
            retard_moyen_s = excluded.retard_moyen_s,
            retard_median_s = excluded.retard_median_s,
            pct_retard_5min = excluded.pct_retard_5min,
            pct_avance_1min = excluded.pct_avance_1min,
            n_arrets_sautes = excluded.n_arrets_sautes,
            computed_at = excluded.computed_at
    """, rows)
    conn.commit()
    
def main():
    conn = sqlite3.connect(DB_PATH)
    df = load_completed_observations(conn)
    print(f"{len(df)} observations terminées chargées.")
    
    stats = compute_line_stats(df)
    pd.set_option("display.float_format", "{:.1f}".format)
    print(stats.to_string(index=False))
    
    stat_date = date.today().isoformat()
    save_stats_to_db(conn, stats, stat_date)
    print(f"Stats sauvegardées pour {stat_date}")

    conn.close()


if __name__ == "__main__":
    main()