"""Tableau de bord de fiabilite des passages TBM.

Charte graphique TBM alignee sur les rapports mensuels (fond clair, bleu #009EE3,
vert #94C21E, magenta #E7007C, orange #F5A623). Les observations les plus recentes
restent dans le flux GTFS-RT : elles sont ecartees afin de ne mesurer que des
passages pour lesquels le retard est stabilise.
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
    network_daily_chart,
    network_hourly_chart,
    mode_comparison_chart,
    mode_daily_chart,
    mode_hourly_chart,
    timeline_chart,
    hourly_risk_chart,
    delay_distribution_chart,
    collection_minutely_chart,
    hourly_distribution_chart,
)

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "vigie_tbm.db"
FRESHNESS_BUFFER_SECONDS = 20 * 60
MIN_OBSERVATIONS_DEFAULT = 100

TBM_BLEU = "#009EE3"
TBM_VERT = "#94C21E"
TBM_MAGENTA = "#E7007C"
TBM_ORANGE = "#F5A623"
TBM_GRIS = "#E8E9EB"
TBM_GRIS_TEXTE = "#4A4A4A"

MODE_LABELS = {0: "Tramway", 3: "Bus", 4: "Ferry"}
MODE_COLORS = {0: TBM_BLEU, 3: TBM_VERT, 4: TBM_MAGENTA}
CAUSE_LABELS = {
    1: "Inconnu", 2: "Autre", 3: "Problème technique", 4: "Grève", 5: "Demande",
    6: "Météo", 7: "Maintenance", 8: "Travaux", 9: "Activité de police",
    10: "Urgence médicale", 11: "Accident",
}


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
        .stApp {{ background: #f4f6f9; color: #2b3038; }}
        [data-testid="stHeader"] {{ background: transparent; }}
        [data-testid="stSidebar"] {{ background: #ffffff; border-right: 1px solid #e2e6ec; }}
        [data-testid="stSidebar"] * {{ color: #3a414b; }}
        .block-container {{ max-width: 1440px; padding-top: 2.1rem; padding-bottom: 3rem; }}
        h1, h2, h3 {{ color: #17181a !important; letter-spacing: -.025em; }}
        h1 {{ font-size: 2.3rem !important; margin-bottom: .15rem !important; }}
        [data-testid="stMetric"] {{ background: #ffffff; border: 1px solid #e2e6ec; border-radius: 12px; padding: 1rem; box-shadow: 0 1px 3px rgba(20, 40, 80, .06); }}
        [data-testid="stMetricLabel"] {{ color: {TBM_GRIS_TEXTE}; }}
        [data-testid="stMetricValue"] {{ color: #17181a; font-size: 1.65rem; }}
        .eyebrow {{ color: {TBM_BLEU}; font-size: .76rem; text-transform: uppercase; letter-spacing: .15em; font-weight: 700; }}
        .hero-subtitle {{ color: #5b6570; font-size: 1.02rem; margin-bottom: 1.4rem; }}
        .section-note {{ color: #5b6570; font-size: .88rem; margin-top: -.45rem; margin-bottom: .75rem; }}
        .insight {{ background: #ffffff; border-left: 3px solid {TBM_BLEU}; border-radius: 8px; padding: .8rem 1rem; color: #2b3038; box-shadow: 0 1px 3px rgba(20, 40, 80, .06); }}
        .stTabs [data-baseweb="tab-list"] {{ gap: 1.3rem; border-bottom: 1px solid #e2e6ec; }}
        .stTabs [data-baseweb="tab"] {{ color: #5b6570; padding: .55rem .15rem; }}
        .stTabs [aria-selected="true"] {{ color: {TBM_BLEU}; border-bottom-color: {TBM_BLEU}; }}
        .stDataFrame {{ border: 1px solid #e2e6ec; border-radius: 10px; overflow: hidden; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def load_network_data(conn: sqlite3.Connection, cutoff_ts: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    scheduled = pd.read_sql_query(
        """
        SELECT o.route_id, COALESCE(r.route_short_name, o.route_id) AS ligne,
               r.route_type,
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
               r.route_type,
               SUM(CASE WHEN o.schedule_relationship = 'SKIPPED' THEN 1 ELSE 0 END) AS skipped,
               COUNT(*) AS eligible
        FROM observations o LEFT JOIN routes r ON r.route_id = o.route_id
        WHERE o.last_seen_at < ? AND o.schedule_relationship IN ('SCHEDULED', 'SKIPPED')
        GROUP BY o.route_id, ligne, r.route_type
        """,
        conn, params=(cutoff_ts,),
    )
    return scheduled, skipped


def make_ranking(scheduled: pd.DataFrame, skipped: pd.DataFrame) -> pd.DataFrame:
    grouped = scheduled.groupby(["route_id", "ligne", "route_type"], as_index=False).agg(
        observations=("departure_delay", "size"),
        retard_moyen_s=("departure_delay", "mean"),
        retard_median_s=("departure_delay", "median"),
        pct_a_l_heure=("departure_delay", lambda x: (x <= 300).mean() * 100),
        pct_retard_5min=("departure_delay", lambda x: (x > 300).mean() * 100),
        pct_avance_1min=("departure_delay", lambda x: (x < -60).mean() * 100),
    )
    ranking = grouped.merge(skipped, on=["route_id", "ligne", "route_type"], how="left").fillna({"skipped": 0, "eligible": 0})
    ranking["pct_arrets_sautes"] = np.where(ranking["eligible"] > 0, ranking["skipped"] / ranking["eligible"] * 100, 0)
    ranking["score_fiabilite"] = (ranking["pct_a_l_heure"] - ranking["pct_arrets_sautes"] * 2).clip(0, 100)
    ranking["mode"] = ranking["route_type"].map(MODE_LABELS).fillna("Autre")
    ranking["mode_color"] = ranking["route_type"].map(MODE_COLORS).fillna(TBM_GRIS_TEXTE)
    return ranking.sort_values(["score_fiabilite", "observations"], ascending=[True, False])


def make_mode_stats(scheduled: pd.DataFrame, skipped: pd.DataFrame) -> pd.DataFrame:
    g = scheduled.groupby("route_type").agg(
        observations=("departure_delay", "size"),
        retard_moyen_s=("departure_delay", "mean"),
        retard_median_s=("departure_delay", "median"),
        pct_a_l_heure=("departure_delay", lambda x: (x <= 300).mean() * 100),
        pct_retard_5min=("departure_delay", lambda x: (x > 300).mean() * 100),
        pct_avance_1min=("departure_delay", lambda x: (x < -60).mean() * 100),
    ).reset_index()
    sk = skipped.groupby("route_type").agg(skipped=("skipped", "sum"), eligible=("eligible", "sum")).reset_index()
    g = g.merge(sk, on="route_type", how="left").fillna({"skipped": 0, "eligible": 0})
    g["pct_arrets_sautes"] = np.where(g["eligible"] > 0, g["skipped"] / g["eligible"] * 100, 0)
    g["mode"] = g["route_type"].map(MODE_LABELS).fillna("Autre")
    g["mode_color"] = g["route_type"].map(MODE_COLORS).fillna(TBM_GRIS_TEXTE)
    return g.sort_values("observations", ascending=False)


def load_network_daily(conn: sqlite3.Connection, cutoff_ts: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT date(datetime(departure_time, 'unixepoch', 'localtime')) AS date_service,
               COUNT(*) AS observations,
               AVG(departure_delay) AS retard_moyen_s,
               AVG(CASE WHEN departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100 AS pct_retard_5min
        FROM observations
        WHERE last_seen_at < ? AND schedule_relationship = 'SCHEDULED'
              AND departure_delay IS NOT NULL AND departure_time IS NOT NULL
        GROUP BY date_service ORDER BY date_service
        """, conn, params=(cutoff_ts,),
    )
    if not df.empty:
        df["date_service"] = pd.to_datetime(df["date_service"])
    return df


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


def load_hourly(conn: sqlite3.Connection, cutoff_ts: int, route_id: str | None = None) -> pd.DataFrame:
    where = "last_seen_at < ? AND schedule_relationship = 'SCHEDULED' AND departure_delay IS NOT NULL AND departure_time IS NOT NULL"
    params: list = [cutoff_ts]
    if route_id:
        where += " AND route_id = ?"
        params.append(route_id)
    return pd.read_sql_query(
        f"""
        SELECT CAST(strftime('%H', datetime(departure_time, 'unixepoch', 'localtime')) AS INTEGER) AS heure,
               COUNT(*) AS observations, AVG(departure_delay) AS retard_moyen_s,
               AVG(CASE WHEN departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100 AS pct_retard_5min
        FROM observations WHERE {where}
        GROUP BY heure ORDER BY heure
        """, conn, params=tuple(params),
    )


def load_distribution(conn: sqlite3.Connection, cutoff_ts: int, route_id: str | None = None) -> pd.DataFrame:
    where = "last_seen_at < ? AND schedule_relationship = 'SCHEDULED' AND departure_delay IS NOT NULL"
    params: list = [cutoff_ts]
    if route_id:
        where += " AND route_id = ?"
        params.append(route_id)
    delays = pd.read_sql_query(
        f"SELECT departure_delay FROM observations WHERE {where}",
        conn, params=tuple(params),
    )
    if delays.empty:
        return delays
    bins = [-3600, -600, -300, -120, -60, 0, 60, 120, 300, 600, 1200, 3601]
    labels = ["< −10 min", "−10 à −5", "−5 à −2", "−2 à −1", "−1 à 0", "0 à +1", "+1 à +2", "+2 à +5", "+5 à +10", "+10 à +20", "> +20 min"]
    bucket = pd.cut(delays["departure_delay"].clip(-3600, 3600), bins=bins, labels=labels, right=False)
    return bucket.value_counts(sort=False).rename_axis("plage").reset_index(name="observations")


def load_mode_daily(conn: sqlite3.Connection, cutoff_ts: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT date(datetime(o.departure_time, 'unixepoch', 'localtime')) AS date_service,
               r.route_type,
               AVG(CASE WHEN o.departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100 AS pct_retard_5min
        FROM observations o LEFT JOIN routes r ON o.route_id = r.route_id
        WHERE o.last_seen_at < ? AND o.schedule_relationship = 'SCHEDULED'
              AND o.departure_delay IS NOT NULL AND o.departure_time IS NOT NULL
        GROUP BY date_service, r.route_type ORDER BY date_service
        """, conn, params=(cutoff_ts,),
    )
    if not df.empty:
        df["date_service"] = pd.to_datetime(df["date_service"])
        df["mode"] = df["route_type"].map(MODE_LABELS).fillna("Autre")
        df["mode_color"] = df["route_type"].map(MODE_COLORS).fillna(TBM_GRIS_TEXTE)
    return df


def load_mode_hourly(conn: sqlite3.Connection, cutoff_ts: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT CAST(strftime('%H', datetime(o.departure_time, 'unixepoch', 'localtime')) AS INTEGER) AS heure,
               r.route_type,
               AVG(CASE WHEN o.departure_delay > 300 THEN 1.0 ELSE 0.0 END) * 100 AS pct_retard_5min
        FROM observations o LEFT JOIN routes r ON o.route_id = r.route_id
        WHERE o.last_seen_at < ? AND o.schedule_relationship = 'SCHEDULED'
              AND o.departure_delay IS NOT NULL AND o.departure_time IS NOT NULL
        GROUP BY heure, r.route_type ORDER BY heure
        """, conn, params=(cutoff_ts,),
    )
    if not df.empty:
        df["mode"] = df["route_type"].map(MODE_LABELS).fillna("Autre")
        df["mode_color"] = df["route_type"].map(MODE_COLORS).fillna(TBM_GRIS_TEXTE)
    return df


def load_active_alerts(conn: sqlite3.Connection, now_ts: int) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT a.route_id, COALESCE(r.route_short_name, a.route_id) AS ligne,
               a.header_text, a.description_text, a.cause,
               datetime(a.active_period_start, 'unixepoch', 'localtime') AS debut,
               datetime(a.active_period_end, 'unixepoch', 'localtime') AS fin
        FROM service_alerts a LEFT JOIN routes r ON a.route_id = r.route_id
        WHERE a.active_period_start <= ? AND a.active_period_end >= ?
        ORDER BY a.active_period_end DESC
        """, conn, params=(now_ts, now_ts),
    )


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
    cutoff = None if last is None else int(last) - FRESHNESS_BUFFER_SECONDS
    if cutoff is None:
        analysed = 0
    else:
        analysed = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE last_seen_at < ? AND schedule_relationship = 'SCHEDULED' AND departure_delay IS NOT NULL",
            (cutoff,),
        ).fetchone()[0]
    return {
        "hourly": hourly,
        "first_ts": first, "last_ts": last,
        "total": total, "trajets": n_trajets, "lignes": n_lignes, "analysed": analysed,
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
        mode_stats = make_mode_stats(scheduled, skipped)

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
        eligible_total = int(skipped["eligible"].sum())
        skip_rate = skipped_total / max(eligible_total, 1) * 100
        st.markdown('<div class="eyebrow">Observatoire opérationnel · Bordeaux Métropole</div>', unsafe_allow_html=True)
        st.title("La fiabilité du réseau, en un coup d’œil.")
        st.markdown('<div class="hero-subtitle">Des indicateurs lisibles pour identifier les lignes, les modes et les créneaux qui demandent une attention.</div>', unsafe_allow_html=True)
        metrics = st.columns(5)
        metrics[0].metric("Passages analysés", f"{total:,}".replace(",", " "))
        metrics[1].metric("Ponctualité réseau", f"{on_time:.1f} %", "≤ 5 min de retard")
        metrics[2].metric("Retard moyen", format_seconds(scheduled.departure_delay.mean(), signed=True))
        metrics[3].metric("Lignes suivies", f"{len(ranking)}")
        metrics[4].metric("Arrêts sautés", f"{skip_rate:.2f} %", f"{skipped_total:,} / {eligible_total:,} attendus".replace(",", " "))
        st.caption("**Passage analysé** : un départ programmé (SCHEDULED) avec retard connu, sorti du flux depuis ≥ 20 min. **Observation** : une ligne brute du flux GTFS-RT (sert à mesurer le volume de collecte). **Arrêts sautés** : arrêts annoncés SKIPPED, rapportés aux arrêts attendus (SCHEDULED + SKIPPED).")

        tab_overview, tab_modes, tab_line, tab_alerts, tab_collecte, tab_method = st.tabs([
            "Vue réseau", "Modes de transport", "Analyse d'une ligne", "Alertes", "Collecte des données", "Méthode & données",
        ])

        with tab_overview:
            st.markdown("### Priorités de fiabilité")
            st.markdown('<div class="section-note">Le score combine ponctualité (≤ 5 min) et passages signalés comme sautés. Plus il est bas, plus la ligne mérite une attention.</div>', unsafe_allow_html=True)
            left, right = st.columns([1.05, .95], gap="large")
            with left:
                chart_data = visible_ranking.head(15).sort_values("score_fiabilite")
                hc_render(ranking_chart(chart_data), height=390)
            with right:
                st.markdown("#### Carte de risque (retard médian par mode)")
                hc_render(scatter_chart(visible_ranking), height=390)
            worst = ranking.iloc[0]
            st.markdown(f'<div class="insight">À surveiller en premier : <b>ligne {worst.ligne}</b> — score de fiabilité {worst.score_fiabilite:.1f}/100, avec {worst.pct_retard_5min:.1f} % de passages au-delà de 5 minutes.</div>', unsafe_allow_html=True)

            st.markdown("### Évolution du réseau")
            daily = load_network_daily(conn, cutoff)
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("#### Retards > 5 min par jour")
                if daily.empty or len(daily) < 2:
                    st.info("L'évolution apparaîtra dès que plusieurs jours de données seront disponibles.")
                else:
                    hc_render(network_daily_chart(daily), height=300)
            with right:
                st.markdown("#### Risque selon l'heure")
                net_hourly = load_hourly(conn, cutoff)
                if net_hourly.empty:
                    st.info("Cette vue nécessite les heures de départ des observations.")
                else:
                    hc_render(network_hourly_chart(net_hourly), height=300)

            st.markdown("#### Profil des retards du réseau")
            distribution = load_distribution(conn, cutoff)
            if not distribution.empty:
                hc_render(delay_distribution_chart(distribution), height=280)

            st.markdown("#### Détail des lignes")
            display = visible_ranking[["ligne", "mode", "score_fiabilite", "pct_a_l_heure", "retard_moyen_s", "retard_median_s", "pct_retard_5min", "pct_arrets_sautes", "observations"]].copy()
            display.columns = ["Ligne", "Mode", "Score / 100", "Ponctualité ≤ 5 min", "Retard moyen (s)", "Retard médian (s)", "Retards > 5 min", "Arrêts sautés", "Passages"]
            st.dataframe(display.style.format({
                "Score / 100": "{:.1f}", "Ponctualité ≤ 5 min": "{:.1f} %", "Retard moyen (s)": "{:.0f}",
                "Retard médian (s)": "{:.0f}", "Retards > 5 min": "{:.1f} %", "Arrêts sautés": "{:.2f} %", "Passages": "{:,}",
            }), use_container_width=True, hide_index=True, height=330)

        with tab_modes:
            st.markdown("### Comparaison par mode de transport")
            st.markdown('<div class="section-note">Tramway, bus et ferry n’ont pas les mêmes contraintes : comparer leurs profils permet d’isoler des problèmes structurels.</div>', unsafe_allow_html=True)
            card_html = '<div style="display:flex;gap:1rem;margin-bottom:.2rem;flex-wrap:wrap">'
            for r in mode_stats.itertuples():
                card_html += (
                    f'<div style="flex:1 1 0;min-width:220px;background:#ffffff;border:1px solid #e2e6ec;'
                    f'border-left:4px solid {r.mode_color};border-radius:10px;padding:.9rem 1rem;box-shadow:0 1px 3px rgba(20,40,80,.06)">'
                    f'<div style="font-size:.8rem;text-transform:uppercase;letter-spacing:.1em;font-weight:600;color:{TBM_GRIS_TEXTE}">{r.mode}</div>'
                    f'<div style="font-size:1.9rem;font-weight:700;color:{r.mode_color};line-height:1.15">{r.pct_a_l_heure:.1f} %</div>'
                    f'<div style="font-size:.82rem;color:#5b6570">{int(r.observations):,} passages · '
                    f'retard médian {format_seconds(r.retard_median_s, signed=True)} · {r.pct_retard_5min:.1f} % &gt; 5 min</div>'
                    f'</div>'
                )
            card_html += '</div>'
            st.markdown(card_html, unsafe_allow_html=True)
            left, right = st.columns(2, gap="large")
            with left:
                hc_render(mode_comparison_chart(mode_stats), height=330)
            with right:
                st.markdown("#### Profil horaire par mode")
                mh = load_mode_hourly(conn, cutoff)
                if mh.empty:
                    st.info("Aucune donnée horaire par mode.")
                else:
                    hc_render(mode_hourly_chart(mh), height=330)
            st.markdown("#### Évolution quotidienne par mode")
            md = load_mode_daily(conn, cutoff)
            if md.empty or md["date_service"].nunique() < 2:
                st.info("L'évolution apparaîtra dès que plusieurs jours de données seront disponibles.")
            else:
                hc_render(mode_daily_chart(md), height=300)

            table = mode_stats[["mode", "observations", "pct_a_l_heure", "pct_retard_5min", "pct_avance_1min", "retard_moyen_s", "retard_median_s", "pct_arrets_sautes"]].copy()
            table.columns = ["Mode", "Passages", "Ponctualité ≤ 5 min", "Retards > 5 min", "En avance > 1 min", "Retard moyen (s)", "Retard médian (s)", "Arrêts sautés"]
            st.dataframe(table.style.format({
                "Passages": "{:,}", "Ponctualité ≤ 5 min": "{:.1f} %", "Retards > 5 min": "{:.1f} %",
                "En avance > 1 min": "{:.1f} %", "Retard moyen (s)": "{:.0f}", "Retard médian (s)": "{:.0f}", "Arrêts sautés": "{:.2f} %",
            }), use_container_width=True, hide_index=True, height=220)

        with tab_line:
            line = ranking[ranking.route_id == selected_route_id].iloc[0]
            st.markdown(f"### Ligne {line['ligne']} · <span style='color:{line['mode_color']}'>{line['mode']}</span>", unsafe_allow_html=True)
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

        with tab_alerts:
            st.markdown("### Alertes en cours sur le réseau")
            st.markdown('<div class="section-note">Messages diffusés par TBM dans le flux GTFS-RT Service Alerts, actifs à l’instant de la consultation.</div>', unsafe_allow_html=True)
            now_ts = int(datetime.now().timestamp())
            alerts = load_active_alerts(conn, now_ts)
            if alerts.empty:
                st.info("Aucune alerte active en ce moment.")
            else:
                alerts["cause"] = alerts["cause"].map(CAUSE_LABELS).fillna("Inconnu")
                a1, a2, a3 = st.columns(3)
                a1.metric("Alertes actives", str(len(alerts)))
                a2.metric("Lignes concernées", str(alerts["ligne"].nunique()))
                top_cause = alerts["cause"].value_counts().idxmax()
                a3.metric("Cause principale", top_cause)
                st.markdown("#### Détail des alertes")
                display = alerts[["ligne", "header_text", "description_text", "cause", "debut", "fin"]].copy()
                display.columns = ["Ligne", "Titre", "Description", "Cause", "Début", "Fin"]
                st.dataframe(display, use_container_width=True, hide_index=True, height=320)

        with tab_collecte:
            stats = load_collection_stats(conn)
            st.markdown("### Suivi de la collecte")
            st.markdown('<div class="section-note">Volume et continuité des données collectées via les flux GTFS-RT TripUpdates.</div>', unsafe_allow_html=True)
            st.caption("**Observation (brute)** : toute ligne reçue du flux, quelle que soit sa nature. **Passage analysé** : observation SCHEDULED avec retard connu, hors 20 dernières minutes — c'est la définition utilisée partout dans la Vue réseau. Les arrêts SKIPPED ne comptent pas comme passages analysés mais sont suivis à part.")
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Observations (brutes)", f"{stats['total']:,}".replace(",", " "))
            c2.metric("Passages analysés", f"{stats['analysed']:,}".replace(",", " "), "stabilisés, horaires < 5 min")
            c3.metric("Trajets distincts", f"{stats['trajets']:,}".replace(",", " "))
            c4.metric("Première date", format_date(stats["first_ts"]))
            c5.metric("Dernière date", format_date(stats["last_ts"]))
            st.markdown('<div class="section-note">Les « Observations brutes » comptent toutes les lignes reçues du flux. Les « Passages analysés » reprennent la définition de la Vue réseau : passages programmés (SCHEDULED) avec retard connu, hors 20 dernières minutes. Les arrêts sautés (SKIPPED) ne sont pas comptés comme passages mais restent suivis séparément.</div>', unsafe_allow_html=True)
            left, right = st.columns(2, gap="large")
            with left:
                st.markdown("#### Observations par minute")
                last_ts = stats["last_ts"]
                if not last_ts:
                    st.info("Aucune donnée disponible.")
                else:
                    ref_ts = int(last_ts)
                    end_ts = (ref_ts // 60) * 60
                    start_ts = end_ts - 7 * 24 * 3600
                    minutely = load_collection_minutely(conn, start_ts, end_ts)
                    if minutely.empty:
                        st.info("Aucune donnée pour cette période.")
                    else:
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
            c1.markdown("**Ponctualité**  \n+Un passage est classé ponctuel lorsqu'il ne dépasse pas 5 minutes de retard. Les passages en avance sont conservés pour montrer la distribution réelle.")
            c2.markdown("**Arrêts sautés**  \n+Les événements `SKIPPED` sont suivis à part : ils ne gonflent pas artificiellement le retard moyen, mais pénalisent le score de fiabilité.")
            st.caption(f"Fenêtre analysée : {total:,} passages programmés stabilisés ; dernier point retenu le {format_date(cutoff)}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
