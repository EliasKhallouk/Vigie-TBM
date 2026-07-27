import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "vigie_tbm.db"
FRESHNESS_BUFFER_SECONDS = 20 * 60
MIN_OBSERVATIONS_DEFAULT = 100


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def get_cutoff_ts(conn: sqlite3.Connection) -> int | None:
    max_ts = conn.execute("SELECT MAX(last_seen_at) AS max_ts FROM observations").fetchone()["max_ts"]
    if max_ts is None:
        return None
    return int(max_ts) - FRESHNESS_BUFFER_SECONDS


def format_gtfs_start_date(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series.astype(str), format="%Y%m%d", errors="coerce")
    return parsed.fillna(pd.to_datetime(series, errors="coerce"))


def load_line_ranking(conn: sqlite3.Connection, cutoff_ts: int, min_observations: int) -> pd.DataFrame:
    query = """
        SELECT
            o.route_id,
            COALESCE(r.route_short_name, o.route_id) AS route_short_name,
            COUNT(*) AS n_observations,
            AVG(CASE WHEN o.departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100.0 AS pct_retard_5min
        FROM observations o
        LEFT JOIN routes r ON r.route_id = o.route_id
        WHERE o.last_seen_at < ?
          AND o.schedule_relationship = 'SCHEDULED'
          AND o.departure_delay IS NOT NULL
        GROUP BY o.route_id, route_short_name
        HAVING COUNT(*) >= ?
        ORDER BY pct_retard_5min DESC, n_observations DESC
    """
    return pd.read_sql_query(query, conn, params=(cutoff_ts, min_observations))


def load_line_options(conn: sqlite3.Connection, cutoff_ts: int) -> pd.DataFrame:
    query = """
        SELECT
            o.route_id,
            COALESCE(r.route_short_name, o.route_id) AS route_short_name,
            COUNT(*) AS n_observations
        FROM observations o
        LEFT JOIN routes r ON r.route_id = o.route_id
        WHERE o.last_seen_at < ?
          AND o.schedule_relationship = 'SCHEDULED'
          AND o.departure_delay IS NOT NULL
        GROUP BY o.route_id, route_short_name
        ORDER BY route_short_name
    """
    return pd.read_sql_query(query, conn, params=(cutoff_ts,))


def load_daily_trend(conn: sqlite3.Connection, cutoff_ts: int, route_id: str) -> pd.DataFrame:
    query = """
        SELECT
            o.start_date,
            COUNT(*) AS n_observations,
            AVG(CASE WHEN o.departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100.0 AS pct_retard_5min,
            AVG(o.departure_delay) AS retard_moyen_s
        FROM observations o
        WHERE o.last_seen_at < ?
          AND o.route_id = ?
          AND o.schedule_relationship = 'SCHEDULED'
          AND o.departure_delay IS NOT NULL
        GROUP BY o.start_date
        ORDER BY o.start_date
    """
    df = pd.read_sql_query(query, conn, params=(cutoff_ts, route_id))
    if not df.empty:
        df["service_date"] = format_gtfs_start_date(df["start_date"])
    return df


def load_hourly_view(conn: sqlite3.Connection, cutoff_ts: int, route_id: str) -> pd.DataFrame:
    query = """
        WITH enriched AS (
            SELECT
                CAST(strftime('%H', datetime(o.departure_time, 'unixepoch', 'localtime')) AS INTEGER) AS hour_local,
                o.departure_delay
            FROM observations o
            WHERE o.last_seen_at < ?
              AND o.route_id = ?
              AND o.schedule_relationship = 'SCHEDULED'
              AND o.departure_delay IS NOT NULL
              AND o.departure_time IS NOT NULL
        )
        SELECT
            CASE
                WHEN hour_local BETWEEN 6 AND 9 THEN 'Matin (06h-09h)'
                WHEN hour_local BETWEEN 10 AND 15 THEN 'Journée (10h-15h)'
                WHEN hour_local BETWEEN 16 AND 19 THEN 'Pointe soir (16h-19h)'
                ELSE 'Nuit / autres'
            END AS tranche_horaire,
            COUNT(*) AS n_observations,
            AVG(CASE WHEN departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100.0 AS pct_retard_5min,
            AVG(departure_delay) AS retard_moyen_s
        FROM enriched
        GROUP BY tranche_horaire
    """
    df = pd.read_sql_query(query, conn, params=(cutoff_ts, route_id))
    if df.empty:
        return df
    order = ["Matin (06h-09h)", "Journée (10h-15h)", "Pointe soir (16h-19h)", "Nuit / autres"]
    df["tranche_horaire"] = pd.Categorical(df["tranche_horaire"], categories=order, ordered=True)
    return df.sort_values("tranche_horaire")


def load_delay_distribution(conn: sqlite3.Connection, cutoff_ts: int, route_id: str) -> pd.DataFrame:
    query = """
        SELECT o.departure_delay
        FROM observations o
        WHERE o.last_seen_at < ?
          AND o.route_id = ?
          AND o.schedule_relationship = 'SCHEDULED'
          AND o.departure_delay IS NOT NULL
    """
    df = pd.read_sql_query(query, conn, params=(cutoff_ts, route_id))
    if df.empty:
        return df

    bins = [-3600, -600, -300, -120, -60, 0, 60, 120, 300, 600, 1200, 3600]
    labels = [
        "< -10 min",
        "-10 à -5 min",
        "-5 à -2 min",
        "-2 à -1 min",
        "-1 à 0 min",
        "0 à +1 min",
        "+1 à +2 min",
        "+2 à +5 min",
        "+5 à +10 min",
        "+10 à +20 min",
        "> +20 min",
    ]
    clipped = df["departure_delay"].clip(lower=bins[0], upper=bins[-1] - 1)
    categories = pd.cut(clipped, bins=bins, labels=labels, right=False)
    histogram = categories.value_counts(sort=False).rename_axis("classe_retard").reset_index(name="n_observations")
    return histogram


def load_skipped_rates(conn: sqlite3.Connection, cutoff_ts: int, min_observations: int) -> pd.DataFrame:
    query = """
        SELECT
            o.route_id,
            COALESCE(r.route_short_name, o.route_id) AS route_short_name,
            SUM(CASE WHEN o.schedule_relationship = 'SKIPPED' THEN 1 ELSE 0 END) AS n_arrets_sautes,
            SUM(CASE WHEN o.schedule_relationship IN ('SCHEDULED', 'SKIPPED') THEN 1 ELSE 0 END) AS n_passages_eligibles,
            AVG(CASE WHEN o.schedule_relationship = 'SKIPPED' THEN 1.0 ELSE 0.0 END) * 100.0 AS pct_arrets_sautes
        FROM observations o
        LEFT JOIN routes r ON r.route_id = o.route_id
        WHERE o.last_seen_at < ?
          AND o.schedule_relationship IN ('SCHEDULED', 'SKIPPED')
        GROUP BY o.route_id, route_short_name
        HAVING n_passages_eligibles >= ?
        ORDER BY pct_arrets_sautes DESC, n_arrets_sautes DESC
    """
    return pd.read_sql_query(query, conn, params=(cutoff_ts, min_observations))


def main() -> None:
    st.set_page_config(page_title="Vigie TBM — Dashboard", layout="wide")
    st.title("Vigie TBM — Tableau de bord fiabilité")
    st.caption("Source: data/vigie_tbm.db (GTFS-RT TripUpdates TBM)")

    if not DB_PATH.exists():
        st.error(f"Base SQLite introuvable: {DB_PATH}")
        return

    conn = get_connection()
    try:
        cutoff_ts = get_cutoff_ts(conn)
        if cutoff_ts is None:
            st.warning("Aucune observation disponible pour le moment.")
            return

        st.sidebar.header("Filtres")
        min_observations = st.sidebar.slider(
            "Minimum d'observations par ligne",
            min_value=20,
            max_value=1000,
            value=MIN_OBSERVATIONS_DEFAULT,
            step=20,
        )

        ranking = load_line_ranking(conn, cutoff_ts, min_observations)
        st.subheader("1) Classement des lignes par manque de fiabilité")
        st.caption("Tri: % de passages avec retard > 5 minutes (du pire au meilleur).")
        if ranking.empty:
            st.info("Aucune ligne ne respecte actuellement le seuil d'observations.")
        else:
            st.dataframe(ranking, use_container_width=True, hide_index=True)
            st.bar_chart(
                ranking.set_index("route_short_name")["pct_retard_5min"],
                use_container_width=True,
            )

        line_options = load_line_options(conn, cutoff_ts)
        if line_options.empty:
            st.warning("Aucune ligne exploitable pour l'analyse détaillée.")
            return

        option_map = {
            f"{row.route_short_name} ({row.route_id}) — {int(row.n_observations)} obs": row.route_id
            for row in line_options.itertuples()
        }
        selected_label = st.sidebar.selectbox("Ligne", options=list(option_map.keys()))
        selected_route_id = option_map[selected_label]

        st.subheader("2) Évolution jour par jour du taux de retard (> 5 min)")
        trend = load_daily_trend(conn, cutoff_ts, selected_route_id)
        if trend.empty:
            st.info("Pas de données journalières disponibles pour cette ligne.")
        else:
            trend_display = trend[["service_date", "n_observations", "pct_retard_5min", "retard_moyen_s"]].copy()
            st.dataframe(trend_display, use_container_width=True, hide_index=True)
            st.line_chart(
                trend.set_index("service_date")[["pct_retard_5min"]],
                use_container_width=True,
            )

        st.subheader("3) Fiabilité par tranche horaire")
        if not column_exists(conn, "observations", "departure_time"):
            st.warning(
                "La colonne departure_time est absente. Exécute la migration SQL pour activer cette vue."
            )
        else:
            hourly = load_hourly_view(conn, cutoff_ts, selected_route_id)
            if hourly.empty:
                st.warning(
                    "Pas encore de données departure_time exploitables pour cette ligne "
                    "(les anciennes observations peuvent rester à NULL)."
                )
            else:
                st.dataframe(hourly, use_container_width=True, hide_index=True)
                st.bar_chart(
                    hourly.set_index("tranche_horaire")["pct_retard_5min"],
                    use_container_width=True,
                )

        st.subheader("4) Distribution des retards")
        delay_hist = load_delay_distribution(conn, cutoff_ts, selected_route_id)
        if delay_hist.empty:
            st.info("Pas de retards exploitables pour construire l'histogramme.")
        else:
            st.dataframe(delay_hist, use_container_width=True, hide_index=True)
            st.bar_chart(delay_hist.set_index("classe_retard")["n_observations"], use_container_width=True)

        st.subheader("5) Taux d'arrêts sautés par ligne")
        st.caption("Calculé séparément des retards (SKIPPED vs SCHEDULED+SKIPPED).")
        skipped = load_skipped_rates(conn, cutoff_ts, min_observations)
        if skipped.empty:
            st.info("Aucune donnée SKIPPED disponible avec le filtre actuel.")
        else:
            st.dataframe(skipped, use_container_width=True, hide_index=True)
            st.bar_chart(
                skipped.set_index("route_short_name")["pct_arrets_sautes"],
                use_container_width=True,
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
