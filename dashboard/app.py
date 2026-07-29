"""Tableau de bord de fiabilite des passages TBM.

Les observations les plus recentes restent dans le flux GTFS-RT : elles sont
ecartees afin de ne mesurer que des passages pour lesquels le retard est stabilise.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from highcharts import (
    render as hc_render,
    ranking_chart,
    scatter_chart,
    timeline_chart,
    hourly_risk_chart,
    delay_distribution_chart,
    collection_minutely_chart,
    hourly_distribution_chart,
)

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "vigie_tbm.db"
FRESHNESS_BUFFER_SECONDS = 20 * 60
MIN_OBSERVATIONS_DEFAULT = 100
NAVY = "#07162f"
BLUE = "#37a5ff"
MINT = "#35d0aa"
MUTED = "#9bb1d1"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_cutoff_ts(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(last_seen_at) AS max_ts FROM observations").fetchone()
    return None if row["max_ts"] is None else int(row["max_ts"]) - FRESHNESS_BUFFER_SECONDS


def format_seconds(value: float | int | None, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "—"
    value = int(round(float(value)))
    sign = "+" if signed and value > 0 else "−" if value < 0 else ""
    absolute = abs(value)
    minutes, seconds = divmod(absolute, 60)
    return f"{sign}{minutes} min {seconds:02d} s" if minutes else f"{sign}{seconds} s"


def format_date(ts: int | None) -> str:
    if not ts:
        return "inconnue"
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y à %H:%M")


def inject_style() -> None:
    st.markdown(
        f"""
        <style>
        .stApp {{ background: radial-gradient(circle at 85% -10%, #123c79 0, {NAVY} 38%, #061126 100%); color: #edf5ff; }}
        [data-testid="stHeader"] {{ background: transparent; }}
        [data-testid="stSidebar"] {{ background: #081a37; border-right: 1px solid #173969; }}
        [data-testid="stSidebar"] * {{ color: #eaf2ff; }}
        .block-container {{ max-width: 1440px; padding-top: 2.1rem; padding-bottom: 3rem; }}
        h1, h2, h3 {{ color: #f5f9ff !important; letter-spacing: -.025em; }}
        h1 {{ font-size: 2.3rem !important; margin-bottom: .15rem !important; }}
        [data-testid="stMetric"] {{ background: linear-gradient(145deg, #102c58, #0b2043); border: 1px solid #1a477e; border-radius: 14px; padding: 1rem; }}
        [data-testid="stMetricLabel"] {{ color: {MUTED}; }}
        [data-testid="stMetricValue"] {{ color: #f6faff; font-size: 1.65rem; }}
        .eyebrow {{ color: {BLUE}; font-size: .76rem; text-transform: uppercase; letter-spacing: .15em; font-weight: 700; }}
        .hero-subtitle {{ color: {MUTED}; font-size: 1.02rem; margin-bottom: 1.4rem; }}
        .section-note {{ color: {MUTED}; font-size: .88rem; margin-top: -.45rem; margin-bottom: .75rem; }}
        .insight {{ background: linear-gradient(100deg, #102e5a, #0d2348); border-left: 3px solid {MINT}; border-radius: 8px; padding: .8rem 1rem; color: #dbeaff; }}
        .stTabs [data-baseweb="tab-list"] {{ gap: 1.3rem; border-bottom: 1px solid #1b3e71; }}
        .stTabs [data-baseweb="tab"] {{ color: {MUTED}; padding: .55rem .15rem; }}
        .stTabs [aria-selected="true"] {{ color: #fff; border-bottom-color: {BLUE}; }}
        .stDataFrame {{ border: 1px solid #1a4173; border-radius: 10px; overflow: hidden; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def load_network_data(conn: sqlite3.Connection, cutoff_ts: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    scheduled = pd.read_sql_query(
        """
        SELECT o.route_id, COALESCE(r.route_short_name, o.route_id) AS ligne,
               o.departure_delay, o.departure_time, o.start_date
        FROM observations o LEFT JOIN routes r ON r.route_id = o.route_id
        WHERE o.last_seen_at < ? AND o.schedule_relationship = 'SCHEDULED'
              AND o.departure_delay IS NOT NULL
        """,
        conn, params=(cutoff_ts,),
    )
    skipped = pd.read_sql_query(
        """
        SELECT o.route_id, COALESCE(r.route_short_name, o.route_id) AS ligne,
               SUM(CASE WHEN o.schedule_relationship = 'SKIPPED' THEN 1 ELSE 0 END) AS skipped,
               COUNT(*) AS eligible
        FROM observations o LEFT JOIN routes r ON r.route_id = o.route_id
        WHERE o.last_seen_at < ? AND o.schedule_relationship IN ('SCHEDULED', 'SKIPPED')
        GROUP BY o.route_id, ligne
        """,
        conn, params=(cutoff_ts,),
    )
    return scheduled, skipped


def make_ranking(scheduled: pd.DataFrame, skipped: pd.DataFrame) -> pd.DataFrame:
    grouped = scheduled.groupby(["route_id", "ligne"], as_index=False).agg(
        observations=("departure_delay", "size"),
        retard_moyen_s=("departure_delay", "mean"),
        retard_median_s=("departure_delay", "median"),
        pct_a_l_heure=("departure_delay", lambda x: (x <= 300).mean() * 100),
        pct_retard_5min=("departure_delay", lambda x: (x > 300).mean() * 100),
        pct_avance_1min=("departure_delay", lambda x: (x < -60).mean() * 100),
    )
    ranking = grouped.merge(skipped, on=["route_id", "ligne"], how="left").fillna({"skipped": 0, "eligible": 0})
    ranking["pct_arrets_sautes"] = np.where(ranking["eligible"] > 0, ranking["skipped"] / ranking["eligible"] * 100, 0)
    # Score lisible : ponctualite d'abord, passages sautes ensuite.
    ranking["score_fiabilite"] = (ranking["pct_a_l_heure"] - ranking["pct_arrets_sautes"] * 2).clip(0, 100)
    return ranking.sort_values(["score_fiabilite", "observations"], ascending=[True, False])


def load_line_timeline(conn: sqlite3.Connection, cutoff_ts: int, route_id: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT date(datetime(departure_time, 'unixepoch', 'localtime')) AS date_service,
               COUNT(*) AS observations, AVG(departure_delay) AS retard_moyen_s,
               AVG(CASE WHEN departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100 AS pct_retard_5min
        FROM observations
        WHERE last_seen_at < ? AND route_id = ? AND schedule_relationship = 'SCHEDULED'
              AND departure_delay IS NOT NULL AND departure_time IS NOT NULL
        GROUP BY date_service ORDER BY date_service
        """, conn, params=(cutoff_ts, route_id),
    )
    if not df.empty:
        df["date_service"] = pd.to_datetime(df["date_service"])
    return df


def load_hourly(conn: sqlite3.Connection, cutoff_ts: int, route_id: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT CAST(strftime('%H', datetime(departure_time, 'unixepoch', 'localtime')) AS INTEGER) AS heure,
               COUNT(*) AS observations, AVG(departure_delay) AS retard_moyen_s,
               AVG(CASE WHEN departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100 AS pct_retard_5min
        FROM observations
        WHERE last_seen_at < ? AND route_id = ? AND schedule_relationship = 'SCHEDULED'
              AND departure_delay IS NOT NULL AND departure_time IS NOT NULL
        GROUP BY heure ORDER BY heure
        """, conn, params=(cutoff_ts, route_id),
    )


def load_distribution(conn: sqlite3.Connection, cutoff_ts: int, route_id: str) -> pd.DataFrame:
    delays = pd.read_sql_query(
        """SELECT departure_delay FROM observations WHERE last_seen_at < ? AND route_id = ?
           AND schedule_relationship = 'SCHEDULED' AND departure_delay IS NOT NULL""",
        conn, params=(cutoff_ts, route_id),
    )
    if delays.empty:
        return delays
    bins = [-3600, -600, -300, -120, -60, 0, 60, 120, 300, 600, 1200, 3601]
    labels = ["< −10 min", "−10 à −5", "−5 à −2", "−2 à −1", "−1 à 0", "0 à +1", "+1 à +2", "+2 à +5", "+5 à +10", "+10 à +20", "> +20 min"]
    bucket = pd.cut(delays["departure_delay"].clip(-3600, 3600), bins=bins, labels=labels, right=False)
    return bucket.value_counts(sort=False).rename_axis("plage").reset_index(name="observations")


def load_collection_stats(conn: sqlite3.Connection) -> dict:
    hourly = pd.read_sql_query(
        """
        SELECT CAST(strftime('%H', datetime(last_seen_at, 'unixepoch', 'localtime')) AS INTEGER) AS heure,
               COUNT(*) AS observations
        FROM observations
        GROUP BY heure ORDER BY heure
        """, conn,
    )
    first = conn.execute("SELECT MIN(last_seen_at) FROM observations").fetchone()[0]
    last = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    n_trajets = conn.execute("SELECT COUNT(DISTINCT trip_id || start_date) FROM observations").fetchone()[0]
    n_lignes = conn.execute("SELECT COUNT(DISTINCT route_id) FROM observations").fetchone()[0]
    return {
        "hourly": hourly,
        "first_ts": first, "last_ts": last,
        "total": total, "trajets": n_trajets, "lignes": n_lignes,
    }


@st.cache_data(ttl=60)
def load_collection_minutely(_conn, start_ts, end_ts):
    df = pd.read_sql_query(
        """
        SELECT datetime(
            CAST(last_seen_at / 60 AS INTEGER) * 60,
            'unixepoch', 'localtime'
        ) AS minute,
        COUNT(*) AS observations
        FROM observations
        WHERE last_seen_at >= ? AND last_seen_at < ?
        GROUP BY minute ORDER BY minute
        """,
        _conn, params=(start_ts, end_ts),
    )
    if df.empty:
        return df
    df["minute"] = pd.to_datetime(df["minute"])
    debut = datetime.fromtimestamp(start_ts).replace(second=0, microsecond=0)
    fin = datetime.fromtimestamp(end_ts).replace(second=0, microsecond=0)
    idx = pd.date_range(start=debut, end=fin, freq="min")
    df = df.set_index("minute").reindex(idx).fillna(0).rename_axis("minute").reset_index()
    return df


def main() -> None:
    st.set_page_config(page_title="Vigie TBM | Fiabilité", page_icon="◉", layout="wide")
    inject_style()
    if not DB_PATH.exists():
        st.error(f"Base SQLite introuvable : {DB_PATH}")
        return
    conn = get_connection()
    try:
        cutoff = get_cutoff_ts(conn)
        if cutoff is None:
            st.warning("Aucune observation disponible pour le moment.")
            return
        scheduled, skipped = load_network_data(conn, cutoff)
        if scheduled.empty:
            st.warning("Aucun passage exploitable après stabilisation des données.")
            return
        ranking = make_ranking(scheduled, skipped)

        st.sidebar.markdown("## Vigie TBM")
        st.sidebar.caption("Pilotage de la ponctualité")
        min_observations = st.sidebar.slider("Seuil d'échantillon", 20, 1000, MIN_OBSERVATIONS_DEFAULT, 20)
        visible_ranking = ranking[ranking["observations"] >= min_observations].copy()
        options = visible_ranking if not visible_ranking.empty else ranking
        route_labels = {f"Ligne {r.ligne} · {int(r.observations):,} passages": r.route_id for r in options.itertuples()}
        selected_label = st.sidebar.selectbox("Ligne analysée", list(route_labels), index=0)
        selected_route_id = route_labels[selected_label]
        st.sidebar.markdown("---")
        st.sidebar.caption(f"Données arrêtées au {format_date(cutoff)}\n\nLes 20 dernières minutes sont exclues pour éviter les retards encore mouvants.")

        total = len(scheduled)
        on_time = (scheduled["departure_delay"] <= 300).mean() * 100
        delayed = (scheduled["departure_delay"] > 300).mean() * 100
        skipped_total = int(skipped["skipped"].sum())
        skip_rate = skipped_total / max(int(skipped["eligible"].sum()), 1) * 100
        st.markdown('<div class="eyebrow">Observatoire opérationnel · Bordeaux Métropole</div>', unsafe_allow_html=True)
        st.title("La fiabilité du réseau, en un coup d’œil.")
        st.markdown('<div class="hero-subtitle">Des indicateurs lisibles pour identifier les lignes et créneaux qui demandent une attention.</div>', unsafe_allow_html=True)
        metrics = st.columns(5)
        metrics[0].metric("Passages analysés", f"{total:,}".replace(",", " "))
        metrics[1].metric("Ponctualité réseau", f"{on_time:.1f} %", "≤ 5 min de retard")
        metrics[2].metric("Retard moyen", format_seconds(scheduled.departure_delay.mean(), signed=True))
        metrics[3].metric("Lignes suivies", f"{len(ranking)}")
        metrics[4].metric("Arrêts sautés", f"{skip_rate:.2f} %", f"{skipped_total:,}".replace(",", " "))

        tab_overview, tab_line, tab_collecte, tab_method = st.tabs(["Vue réseau", "Analyse d'une ligne", "Collecte des données", "Méthode & données"])
        with tab_overview:
            st.markdown("### Priorités de fiabilité")
            st.markdown('<div class="section-note">Le score combine ponctualité (≤ 5 min) et passages signalés comme sautés. Plus il est bas, plus la ligne mérite une attention.</div>', unsafe_allow_html=True)
            left, right = st.columns([1.05, .95], gap="large")
            with left:
                chart_data = visible_ranking.head(15).sort_values("score_fiabilite")
                hc_render(ranking_chart(chart_data), height=390)
            with right:
                hc_render(scatter_chart(visible_ranking), height=390)
            worst = ranking.iloc[0]
            st.markdown(f'<div class="insight">À surveiller en premier : <b>ligne {worst.ligne}</b> — score de fiabilité {worst.score_fiabilite:.1f}/100, avec {worst.pct_retard_5min:.1f} % de passages au-delà de 5 minutes.</div>', unsafe_allow_html=True)
            st.markdown("#### Détail des lignes")
            display = visible_ranking[["ligne", "score_fiabilite", "pct_a_l_heure", "retard_moyen_s", "pct_retard_5min", "pct_arrets_sautes", "observations"]].copy()
            display.columns = ["Ligne", "Score / 100", "Ponctualité ≤ 5 min", "Retard moyen (s)", "Retards > 5 min", "Arrêts sautés", "Passages"]
            st.dataframe(display.style.format({"Score / 100": "{:.1f}", "Ponctualité ≤ 5 min": "{:.1f} %", "Retard moyen (s)": "{:.0f}", "Retards > 5 min": "{:.1f} %", "Arrêts sautés": "{:.2f} %", "Passages": "{:,}"}), use_container_width=True, hide_index=True, height=330)

        with tab_line:
            line = ranking[ranking.route_id == selected_route_id].iloc[0]
            st.markdown(f"### Ligne {line.ligne}")
            line_metrics = st.columns(4)
            line_metrics[0].metric("Score de fiabilité", f"{line.score_fiabilite:.1f} / 100")
            line_metrics[1].metric("Retard médian", format_seconds(line.retard_median_s, signed=True))
            line_metrics[2].metric("Passages > 5 min", f"{line.pct_retard_5min:.1f} %")
            line_metrics[3].metric("En avance > 1 min", f"{line.pct_avance_1min:.1f} %")
            timeline = load_line_timeline(conn, cutoff, selected_route_id)
            hourly = load_hourly(conn, cutoff, selected_route_id)
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("#### Évolution quotidienne")
                if timeline.empty or len(timeline) < 2:
                    st.info("L'évolution apparaîtra dès que plusieurs jours de données seront disponibles.")
                else:
                    hc_render(timeline_chart(timeline), height=285)
            with right:
                st.markdown("#### Risque selon l'heure")
                if hourly.empty:
                    st.info("Cette vue nécessite les heures de départ des observations.")
                else:
                    hc_render(hourly_risk_chart(hourly, delayed), height=285)
            st.markdown("#### Profil des retards")
            distribution = load_distribution(conn, cutoff, selected_route_id)
            if not distribution.empty:
                hc_render(delay_distribution_chart(distribution), height=280)

        with tab_collecte:
            stats = load_collection_stats(conn)
            st.markdown("### Suivi de la collecte")
            st.markdown('<div class="section-note">Volume et continuité des données collectées via les flux GTFS-RT TripUpdates.</div>', unsafe_allow_html=True)
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Observations", f"{stats['total']:,}".replace(",", " "))
            c2.metric("Trajets distincts", f"{stats['trajets']:,}".replace(",", " "))
            c3.metric("Lignes suivies", str(stats["lignes"]))
            c4.metric("Première date", format_date(stats["first_ts"]))
            c5.metric("Dernière date", format_date(stats["last_ts"]))
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("#### Observations par minute")
                last_ts = stats["last_ts"]
                if not last_ts:
                    st.info("Aucune donnée disponible.")
                else:
                    now_ts = int(datetime.now().timestamp())
                    ref_ts = max(last_ts, now_ts)
                    preset_duration = {
                        "Dernière heure": 3600,
                        "Dernières 6 heures": 6 * 3600,
                        "Dernières 24 heures": 24 * 3600,
                        "Dernière semaine": 7 * 24 * 3600,
                    }
                    options = list(preset_duration.keys()) + ["Personnalisé"]
                    selected = st.selectbox("Période", options, index=2, label_visibility="collapsed")

                    if selected == "Personnalisé":
                        col_a, col_b = st.columns(2)
                        with col_a:
                            d1 = st.date_input("Du", value=datetime.fromtimestamp(ref_ts - 86400).date())
                        with col_b:
                            d2 = st.date_input("Au", value=datetime.fromtimestamp(ref_ts).date())
                        start_ts = int(datetime.combine(d1, datetime.min.time()).timestamp())
                        end_ts = int(datetime.combine(d2, datetime.max.time()).timestamp()) - 60
                    else:
                        duration = preset_duration[selected]
                        end_ts = ref_ts - 60
                        start_ts = end_ts - duration

                    minutely = load_collection_minutely(conn, start_ts, end_ts)

                    if minutely.empty:
                        st.info("Aucune donnée pour cette période.")
                    else:
                        #st.write(minutely.head(10))
                        #st.write(minutely.dtypes)
                        #st.write(minutely["observations"].max())
                        hc_render(collection_minutely_chart(minutely), height=340, use_stock=True)
            with right:
                st.markdown("#### Répartition horaire")
                hourly = stats["hourly"]
                if hourly.empty:
                    st.info("Aucune donnée horaire.")
                else:
                    hc_render(hourly_distribution_chart(hourly), height=280)

        with tab_method:
            st.markdown("### Ce que mesure ce tableau de bord")
            st.markdown("Les données viennent des flux GTFS-RT **TripUpdates** TBM. Une observation est considérée stabilisée après 20 minutes hors du flux temps réel ; cela évite d'interpréter comme final un retard qui peut encore évoluer.")
            c1, c2 = st.columns(2)
            c1.markdown("**Ponctualité**  \\n+Un passage est classé ponctuel lorsqu'il ne dépasse pas 5 minutes de retard. Les passages en avance sont conservés pour montrer la distribution réelle.")
            c2.markdown("**Arrêts sautés**  \\n+Les événements `SKIPPED` sont suivis à part : ils ne gonflent pas artificiellement le retard moyen, mais pénalisent le score de fiabilité.")
            st.caption(f"Fenêtre analysée : {total:,} passages programmés stabilisés ; dernier point retenu le {format_date(cutoff)}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
