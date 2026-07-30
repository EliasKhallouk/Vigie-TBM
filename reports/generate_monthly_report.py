#!/usr/bin/env python3
"""Génère un rapport mensuel Vigie TBM au format LaTeX/PDF.

Le document commence volontairement par une synthèse exécutive d'une page :
elle est destinée aux décideurs. Les tableaux complets sont reportés en annexe.
Une version territoriale s'obtient en fournissant un profil de destinataire
dont les lignes ont été vérifiées (voir recipients.example.json).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

matplotlib.rcParams.update({
    "font.family": "Inter",
    "font.size": 9,
    "axes.unicode_minus": False,
})
plt.rcParams["axes.prop_cycle"] = plt.cycler(color=["#009EE3", "#94C21E", "#E7007C", "#4A4A4A", "#E8E9EB"])

TBM_BLEU = "#009EE3"
TBM_VERT = "#94C21E"
TBM_MAGENTA = "#E7007C"
TBM_ORANGE = "#F5A623"
TBM_GRIS = "#E8E9EB"
TBM_GRIS_TEXTE = "#4A4A4A"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "vigie_tbm.db"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "output"
FRESHNESS_BUFFER_SECONDS = 20 * 60
FRENCH_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


@dataclass
class Scope:
    recipient: str
    routes: list[str]
    communes: list[str]
    description: str


def latex(value: object) -> str:
    """Escape arbitrary database/configuration text for a LaTeX text cell."""
    text = str(value) if value is not None else "—"
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
        "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def pct(value: float | None, decimals: int = 1) -> str:
    return "—" if value is None or pd.isna(value) else f"{value:.{decimals}f}\\%"


def number(value: float | int | None, decimals: int = 0) -> str:
    """French thousands separator for LaTeX, without an English comma."""
    if value is None or pd.isna(value):
        return "—"
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", r"\,").replace(".", ",")


def duration(seconds: float | None, signed: bool = True) -> str:
    if seconds is None or pd.isna(seconds):
        return "—"
    seconds = int(round(seconds))
    prefix = "+" if signed and seconds > 0 else "-" if seconds < 0 else ""
    minutes, rest = divmod(abs(seconds), 60)
    return f"{prefix}{minutes} min {rest:02d} s" if minutes else f"{prefix}{rest} s"


def kpi_color(metrics: dict, key: str) -> str:
    """Return a LaTeX color name based on the metric value."""
    if key == "fiability":
        v = metrics[key]
        return "vigiebleu" if v >= 80 else "alert"
    if key == "ponctualite":
        v = metrics[key]
        return "vigiebleu" if v >= 80 else "alert"
    if key in ("retard", "retard_median"):
        v = abs(metrics[key])
        return "vigiebleu" if v <= 120 else "alert"
    if key == "skip_rate":
        v = metrics[key]
        return "vigiebleu" if v <= 5 else "alert"
    return "vigiebleu"


def net_val(network_metrics: dict | None, key: str, formatter) -> str:
    """Return a LaTeX snippet showing the network-wide comparison value, or empty."""
    if network_metrics is None:
        return ""
    return f" {{\\tiny\\color{{gray}}Réseau: {formatter(network_metrics[key])}}}"


def safe_slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-") or "rapport"


def resolve_month(conn: sqlite3.Connection, requested_month: str | None) -> str:
    if requested_month:
        try:
            datetime.strptime(requested_month, "%Y-%m")
        except ValueError as error:
            raise ValueError("Le mois doit respecter le format AAAA-MM.") from error
        return requested_month
    row = conn.execute(
        "SELECT MAX(datetime(departure_time, 'unixepoch', 'localtime')) FROM observations WHERE departure_time IS NOT NULL"
    ).fetchone()
    if not row[0]:
        raise ValueError("Impossible de déterminer le mois : aucune heure de départ n'est disponible.")
    return row[0][:7]


def load_scope(args: argparse.Namespace) -> Scope:
    routes = [route.strip() for route in (args.routes or "").split(",") if route.strip()]
    communes = [commune.strip() for commune in (args.communes or "").split(",") if commune.strip()]
    recipient = args.recipient or "Bordeaux Métropole et TBM"
    description = "Réseau TBM - ensemble des lignes observées"
    if not args.profile:
        if communes:
            description = f"Arrêts géolocalisés dans la commune de {', '.join(communes)}"
        return Scope(recipient, routes, communes, description)

    config_path = Path(args.recipients_file)
    if not config_path.exists():
        raise FileNotFoundError(f"Fichier de profils introuvable : {config_path}")
    profiles = json.loads(config_path.read_text(encoding="utf-8"))
    if args.profile not in profiles:
        choices = ", ".join(profiles) or "aucun"
        raise ValueError(f"Profil '{args.profile}' inconnu. Profils disponibles : {choices}")
    profile = profiles[args.profile]
    profile_routes = profile.get("routes", [])
    profile_communes = profile.get("communes", [])
    if not profile_routes and not profile_communes:
        raise ValueError(f"Le profil '{args.profile}' ne contient ni commune ni ligne. Renseignez-le après vérification.")
    resolved_communes = communes or [str(commune) for commune in profile_communes]
    return Scope(
        profile.get("recipient", args.profile),
        routes or [str(route) for route in profile_routes],
        resolved_communes,
        profile.get(
            "description",
            f"Arrêts géolocalisés dans la commune de {', '.join(resolved_communes)}" if resolved_communes
            else "Périmètre défini par les lignes sélectionnées.",
        ),
    )


def query_observations(conn: sqlite3.Connection, month: str, scope: Scope) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Return scheduled and skipped observations for the calendar month and scope."""
    latest = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    if latest is None:
        raise ValueError("La base ne contient aucune observation.")
    cutoff = int(latest) - FRESHNESS_BUFFER_SECONDS
    route_filter = ""
    params: list[object] = [cutoff, month]
    if scope.routes:
        placeholders = ", ".join("?" for _ in scope.routes)
        route_filter = f" AND o.route_id IN ({placeholders})"
        params.extend(scope.routes)
    commune_filter = ""
    if scope.communes:
        has_mapping = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stop_municipalities')"
        ).fetchone()[0]
        if not has_mapping:
            raise ValueError(
                "Le rattachement des arrêts aux communes n'a pas été calculé. "
                "Exécutez d'abord src/scripts/assign_stop_municipalities.py."
            )
        placeholders = ", ".join("?" for _ in scope.communes)
        available = {
            row[0].casefold(): row[0]
            for row in conn.execute("SELECT DISTINCT commune_name FROM stop_municipalities")
        }
        unknown = [name for name in scope.communes if name.casefold() not in available]
        if unknown:
            raise ValueError(f"Commune(s) inconnue(s) : {', '.join(unknown)}")
        canonical_communes = [available[name.casefold()] for name in scope.communes]
        automatic_description = f"Arrêts géolocalisés dans la commune de {', '.join(scope.communes)}"
        if scope.description == automatic_description:
            scope.description = f"Arrêts géolocalisés dans la commune de {', '.join(canonical_communes)}"
        scope.communes = canonical_communes
        commune_filter = (
            " AND o.stop_id IN (SELECT stop_id FROM stop_municipalities "
            f"WHERE commune_name IN ({placeholders}))"
        )
        params.extend(canonical_communes)

    base = f"""
        FROM observations o
        LEFT JOIN routes r ON r.route_id = o.route_id
        WHERE o.last_seen_at < ?
          AND strftime('%Y-%m', datetime(COALESCE(o.departure_time, o.last_seen_at), 'unixepoch', 'localtime')) = ?
          {route_filter}
          {commune_filter}
    """
    scheduled = pd.read_sql_query(
        "SELECT o.route_id, COALESCE(r.route_short_name, o.route_id) AS ligne, o.departure_delay, o.departure_time "
        + base + " AND o.schedule_relationship = 'SCHEDULED' AND o.departure_delay IS NOT NULL",
        conn, params=params,
    )
    skipped = pd.read_sql_query(
        "SELECT o.route_id, COALESCE(r.route_short_name, o.route_id) AS ligne, "
        "SUM(CASE WHEN o.schedule_relationship = 'SKIPPED' THEN 1 ELSE 0 END) AS skipped, COUNT(*) AS eligible "
        + base + " AND o.schedule_relationship IN ('SCHEDULED', 'SKIPPED') GROUP BY o.route_id, ligne",
        conn, params=params,
    )
    collected_at = datetime.fromtimestamp(cutoff).strftime("%d/%m/%Y à %H:%M")
    return scheduled, skipped, collected_at


def query_stop_stats(conn: sqlite3.Connection, month: str, scope: Scope) -> pd.DataFrame:
    """Return per-stop delay statistics for the given scope, with the dominant ligne."""
    latest = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    if latest is None:
        raise ValueError("La base ne contient aucune observation.")
    cutoff = int(latest) - FRESHNESS_BUFFER_SECONDS
    route_filter = ""
    params: list[object] = [cutoff, month]
    if scope.routes:
        placeholders = ", ".join("?" for _ in scope.routes)
        route_filter = f" AND o.route_id IN ({placeholders})"
        params.extend(scope.routes)
    commune_filter = ""
    if scope.communes:
        placeholders = ", ".join("?" for _ in scope.communes)
        commune_filter = (
            " AND o.stop_id IN (SELECT stop_id FROM stop_municipalities "
            f"WHERE commune_name IN ({placeholders}))"
        )
        params.extend(scope.communes)
    query = f"""
        SELECT o.stop_id, COALESCE(s.stop_name, o.stop_id) AS stop_name,
               o.departure_delay, o.route_id
        FROM observations o
        LEFT JOIN stops s ON o.stop_id = s.stop_id
        WHERE o.last_seen_at < ?
          AND strftime('%Y-%m', datetime(o.departure_time, 'unixepoch', 'localtime')) = ?
          {route_filter}
          {commune_filter}
          AND o.schedule_relationship = 'SCHEDULED'
          AND o.departure_delay IS NOT NULL
    """
    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return pd.DataFrame()
    stats = df.groupby(["stop_id", "stop_name"], as_index=False).agg(
        retard_moyen=("departure_delay", "mean"),
        retard_median=("departure_delay", "median"),
        passages=("departure_delay", "count"),
        main_route=("route_id", lambda xs: xs.value_counts().index[0]),
    ).sort_values("retard_median", ascending=False)
    route_names = pd.read_sql_query("SELECT route_id, route_short_name FROM routes", conn)
    stats = stats.merge(route_names, left_on="main_route", right_on="route_id", how="left")
    return stats


def query_monthly_evolution(conn: sqlite3.Connection, month: str, scope: Scope) -> pd.DataFrame:
    """Return monthly punctuality trend for the scope (all months up to the given one)."""
    latest = conn.execute("SELECT MAX(last_seen_at) FROM observations").fetchone()[0]
    if latest is None:
        return pd.DataFrame()
    cutoff = int(latest) - FRESHNESS_BUFFER_SECONDS
    route_filter = ""
    params: list[object] = [cutoff, month]
    if scope.routes:
        placeholders = ", ".join("?" for _ in scope.routes)
        route_filter = f" AND o.route_id IN ({placeholders})"
        params.extend(scope.routes)
    commune_filter = ""
    if scope.communes:
        placeholders = ", ".join("?" for _ in scope.communes)
        commune_filter = (
            " AND o.stop_id IN (SELECT stop_id FROM stop_municipalities "
            f"WHERE commune_name IN ({placeholders}))"
        )
        params.extend(scope.communes)
    query = f"""
        SELECT strftime('%Y-%m', datetime(o.departure_time, 'unixepoch', 'localtime')) AS mois,
               AVG(CASE WHEN o.departure_delay <= 300 THEN 1.0 ELSE 0.0 END) * 100 AS ponctualite,
               AVG(o.departure_delay) AS retard_moyen,
               COUNT(*) AS passages
        FROM observations o
        WHERE o.last_seen_at < ?
          AND o.schedule_relationship = 'SCHEDULED'
          AND o.departure_delay IS NOT NULL
          AND o.departure_time IS NOT NULL
          AND strftime('%Y-%m', datetime(o.departure_time, 'unixepoch', 'localtime')) <= ?
          {route_filter}
          {commune_filter}
        GROUP BY mois
        ORDER BY mois
    """
    df = pd.read_sql_query(query, conn, params=params)
    return df


def query_collection_gaps(conn: sqlite3.Connection, month: str) -> dict:
    """Return gap stats and total observations for the methodology section."""
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='collection_gaps'"
    ).fetchone()
    if not table_exists:
        total_raw = conn.execute(
            "SELECT COUNT(*) FROM observations o "
            "LEFT JOIN routes r ON r.route_id = o.route_id "
            "WHERE strftime('%Y-%m', datetime(o.departure_time, 'unixepoch', 'localtime')) = ?",
            (month,)
        ).fetchone()[0] or 0
        return {"gap_seconds": 0, "total_raw": int(total_raw)}

    gap_seconds = conn.execute(
        "SELECT COALESCE(SUM(gap_end - gap_start), 0) FROM collection_gaps "
        "WHERE strftime('%Y-%m', datetime(gap_start, 'unixepoch')) = ? "
        "OR strftime('%Y-%m', datetime(gap_end, 'unixepoch')) = ?",
        (month, month)
    ).fetchone()[0] or 0

    total_raw = conn.execute(
        "SELECT COUNT(*) FROM observations o "

        "LEFT JOIN routes r ON r.route_id = o.route_id "

        "WHERE strftime('%Y-%m', datetime(o.departure_time, 'unixepoch', 'localtime')) = ?",

        (month,)
    ).fetchone()[0] or 0

    return {"gap_seconds": int(gap_seconds), "total_raw": int(total_raw)}


def query_service_alerts(conn: sqlite3.Connection, month: str, route_ids: set[str]) -> list[dict]:
    """Return service alerts active during the calendar month for the given route_ids."""
    if not route_ids:
        return []
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='service_alerts'"
    ).fetchone()
    if not table_exists:
        return []
    year, mon = map(int, month.split("-"))
    start_of_month = int(datetime(year, mon, 1, tzinfo=timezone.utc).timestamp())
    last_day = monthrange(year, mon)[1]
    end_of_month = int(datetime(year, mon, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    placeholders = ", ".join("?" for _ in route_ids)
    rows = conn.execute(f"""
        SELECT alert_id, route_id, active_period_start, active_period_end,
               header_text, description_text
        FROM service_alerts
        WHERE active_period_start <= ?
          AND (active_period_end IS NULL OR active_period_end >= ?)
          AND (route_id = '' OR route_id IN ({placeholders}))
        ORDER BY active_period_start DESC
    """, [end_of_month, start_of_month] + list(route_ids)).fetchall()
    return [
        {
            "alert_id": r[0],
            "route_id": r[1],
            "active_period_start": r[2],
            "active_period_end": r[3],
            "header_text": r[4],
            "description_text": r[5],
        }
        for r in rows
    ]


def make_line_stats(scheduled: pd.DataFrame, skipped: pd.DataFrame) -> pd.DataFrame:
    rows = scheduled.groupby(["route_id", "ligne"], as_index=False).agg(
        passages=("departure_delay", "size"),
        retard_moyen=("departure_delay", "mean"),
        retard_median=("departure_delay", "median"),
        ponctualite=("departure_delay", lambda series: (series <= 300).mean() * 100),
        retard_5=("departure_delay", lambda series: (series > 300).mean() * 100),
    )
    rows = rows.merge(skipped, on=["route_id", "ligne"], how="left").fillna({"skipped": 0, "eligible": 0})
    rows["arrets_sautes"] = rows["skipped"] / rows["eligible"].replace(0, 1) * 100
    rows["score"] = (rows["ponctualite"] - 2 * rows["arrets_sautes"]).clip(0, 100)
    return rows.sort_values(["score", "passages"], ascending=[True, False])


def kpis(scheduled: pd.DataFrame, skipped: pd.DataFrame) -> dict[str, float | int]:
    eligible = int(skipped["eligible"].sum()) if not skipped.empty else 0
    skipped_count = int(skipped["skipped"].sum()) if not skipped.empty else 0
    return {
        "passages": len(scheduled),
        "ponctualite": (scheduled.departure_delay <= 300).mean() * 100,
        "retard": scheduled.departure_delay.mean(),
        "retard_median": scheduled.departure_delay.median(),
        "retard_5": (scheduled.departure_delay > 300).mean() * 100,
        "skipped": skipped_count,
        "skip_rate": skipped_count / eligible * 100 if eligible else 0,
        "fiability": max(0.0, (scheduled.departure_delay <= 300).mean() * 100 - 2 * (skipped_count / eligible * 100 if eligible else 0)),
    }


def comparison(current: dict[str, float | int], previous: dict[str, float | int] | None) -> dict[str, str]:
    if not previous:
        return {"fiability": "Historique en cours de constitution — comparaison disponible dès le rapport du mois prochain.",
                "ponctualite": "Historique en cours de constitution — comparaison disponible dès le rapport du mois prochain.",
                "retard": "Historique en cours de constitution — comparaison disponible dès le rapport du mois prochain.",
                "skip_rate": "Historique en cours de constitution — comparaison disponible dès le rapport du mois prochain."}
    return {
        "fiability": f"{float(current['fiability']) - float(previous['fiability']):+.1f} pts vs mois précédent",
        "ponctualite": f"{float(current['ponctualite']) - float(previous['ponctualite']):+.1f} pts vs mois précédent",
        "retard": (
            f"moy. {float(current['retard']) - float(previous['retard']):+.0f} s ; "
            f"méd. {float(current['retard_median']) - float(previous['retard_median']):+.0f} s vs mois précédent"
        ),
        "skip_rate": f"{float(current['skip_rate']) - float(previous['skip_rate']):+.2f} pts vs mois précédent",
    }


def previous_month(month: str) -> str:
    year, month_number = map(int, month.split("-"))
    return f"{year - 1:04d}-12" if month_number == 1 else f"{year:04d}-{month_number - 1:02d}"


def executive_message(metrics: dict[str, float | int], lines: pd.DataFrame, scope: Scope | None = None) -> str:
    p = metrics["ponctualite"]
    skip = metrics["skip_rate"]
    worst = lines.iloc[0]
    prefix = f"Le périmètre {latex(scope.description)} " if scope and scope.communes else "Le réseau TBM "
    if p >= 95:
        assessment = f"{prefix}affiche une ponctualité excellente ({p:.1f}\\%)."
    elif p >= 90:
        assessment = f"{prefix}enregistre un bon niveau de ponctualité ({p:.1f}\\%)."
    elif p >= 85:
        assessment = f"{prefix}présente une fiabilité correcte ({p:.1f}\\%), encore perfectible."
    elif p >= 80:
        assessment = f"{prefix}montre une fiabilité intermédiaire ({p:.1f}\\%)."
    elif p >= 75:
        assessment = f"{prefix}connaît des difficultés de ponctualité notables ({p:.1f}\\%)."
    elif p >= 65:
        assessment = f"{prefix}enregistre une ponctualité insuffisante ({p:.1f}\\%)."
    else:
        assessment = f"{prefix}subit des retards critiques ({p:.1f}\\% de passages à l'heure)."
    if skip > 5:
        assessment += f" Le taux d'arrêts sautés ({skip:.2f}\\% des passages) aggrave la situation."
    return (
        f"{assessment} La principale alerte concerne la ligne {latex(worst.ligne)}, avec un score de fiabilité "
        f"de {worst.score:.1f}/100 et {worst.retard_5:.1f}\\% de passages au-delà de cinq minutes de retard."
    )


def line_table(lines: pd.DataFrame, network_lines: pd.DataFrame | None = None,
               alert_routes: set[str] | None = None) -> str:
    net_lookup: dict[str, tuple] = {}
    if network_lines is not None and not network_lines.empty:
        net_lookup = {row.route_id: row for row in network_lines.itertuples()}
    alert_routes = alert_routes or set()
    table_rows = []
    for row in lines.itertuples():
        net_row = net_lookup.get(row.route_id)
        prefix = r"\alertmark{} " if row.route_id in alert_routes else ""
        ponctualite_cell = pct(row.ponctualite)
        retard_5_cell = pct(row.retard_5)
        arrets_cell = pct(row.arrets_sautes, 2)
        passages_cell = number(int(row.passages))
        if net_row is not None:
            ponctualite_cell += f" {{\\tiny\\color{{gray}}({pct(net_row.ponctualite)})}}"
            retard_5_cell += f" {{\\tiny\\color{{gray}}({pct(net_row.retard_5)})}}"
            arrets_cell += f" {{\\tiny\\color{{gray}}({pct(net_row.arrets_sautes, 2)})}}"
            passages_cell += f" {{\\tiny\\color{{gray}}({number(int(net_row.passages))})}}"
        table_rows.append(
            f"{prefix}{latex(row.ligne)} & {passages_cell} & {ponctualite_cell} & "
            f"{duration(row.retard_moyen)} / {duration(row.retard_median)} & {retard_5_cell} & {arrets_cell} \\\\"
        )
    return "\n".join(table_rows)


def operational_views(scheduled: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the hourly-risk and delay-distribution views shown in the dashboard."""
    dated = scheduled.dropna(subset=["departure_time"]).copy()
    if dated.empty:
        return pd.DataFrame(), pd.DataFrame()
    dated["heure"] = pd.to_datetime(dated["departure_time"], unit="s", utc=True).dt.tz_convert("Europe/Paris").dt.hour
    hourly = dated.groupby("heure", as_index=False).agg(
        passages=("departure_delay", "size"),
        retard_moyen=("departure_delay", "mean"),
        retard_median=("departure_delay", "median"),
        retard_5=("departure_delay", lambda values: (values > 300).mean() * 100),
    )
    all_hours = pd.DataFrame({"heure": range(24)})
    hourly = all_hours.merge(hourly, on="heure", how="left").fillna(
        {"passages": 0, "retard_5": 0.0, "retard_moyen": 0.0, "retard_median": 0.0}
    )
    bins = [-3600, -600, -300, -120, -60, 0, 60, 120, 300, 600, 1200, 3601]
    labels = ["< -10", "-10 a -5", "-5 a -2", "-2 a -1", "-1 a 0", "0 a +1", "+1 a +2", "+2 a +5", "+5 a +10", "+10 a +20", "> +20"]
    classes = pd.cut(dated["departure_delay"].clip(-3600, 3600), bins=bins, labels=labels, right=False)
    distribution = classes.value_counts(sort=False).rename_axis("plage").reset_index(name="passages")
    return hourly, distribution


def _score_color(value: float, thresholds: list[tuple[float, float, str]]) -> str:
    """Return hex color based on thresholds: (low, high, color) tuples.
    The first matching range (low <= value < high) wins."""
    for lo, hi, color in thresholds:
        if lo <= value < hi:
            return color
    return TBM_ORANGE


SCORE_SEUILS = [(80, 101, TBM_VERT), (50, 80, TBM_ORANGE), (0, 50, TBM_MAGENTA)]
RETARD_SEUILS = [(0, 60, TBM_VERT), (60, 180, TBM_ORANGE), (180, float("inf"), TBM_MAGENTA)]
PCT5_SEUILS = [(0, 5, TBM_VERT), (5, 15, TBM_ORANGE), (15, 101, TBM_MAGENTA)]


def _setup_ax(ax: plt.Axes) -> None:
    ax.set_facecolor(TBM_GRIS)
    ax.tick_params(color=TBM_GRIS_TEXTE, labelcolor=TBM_GRIS_TEXTE)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.spines["bottom"].set_visible(True)
    ax.spines["bottom"].set_color(TBM_GRIS_TEXTE + "30")
    ax.spines["left"].set_visible(True)
    ax.spines["left"].set_color(TBM_GRIS_TEXTE + "30")
    ax.grid(axis="y", color=TBM_GRIS_TEXTE, alpha=0.15, linewidth=0.5)
    ax.grid(axis="x", color=TBM_GRIS_TEXTE, alpha=0.15, linewidth=0.5)


def _save_chart(fig: plt.Figure, output_dir: Path, name: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}.png"
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close(fig)
    return path


def reliability_chart(lines: pd.DataFrame, output_dir: Path, name: str,
                      network_lines: pd.DataFrame | None = None) -> Path | None:
    selected = lines.head(15).reset_index(drop=True)
    if selected.empty:
        return None
    fig, ax = plt.subplots(figsize=(7.5, max(2.5, len(selected) * 0.35)))
    _setup_ax(ax)
    colors = [_score_color(float(r.score), SCORE_SEUILS) for _, r in selected.iterrows()]
    y = range(len(selected))
    ax.barh(y, selected["score"], color=colors, height=0.55, zorder=3, edgecolor="white", linewidth=0.3, label="Score")
    if network_lines is not None and not network_lines.empty:
        net_scores = []
        for i, row in selected.iterrows():
            nr = network_lines[network_lines["route_id"] == row.route_id]
            net_scores.append(float(nr.iloc[0]["score"]) if not nr.empty else 0)
        ax.barh(y, net_scores, color=TBM_GRIS_TEXTE, height=0.18, alpha=0.35, zorder=4, label="Réseau")
        ax.legend(fontsize=7, loc="lower right")
    for i, row in selected.iterrows():
        ax.text(float(row.score) + 0.8, i, f"{row.score:.0f}", va="center", fontsize=7, color=TBM_GRIS_TEXTE)
    ax.set_yticks(list(y))
    ax.set_yticklabels(selected["ligne"].tolist(), fontsize=7)
    ax.set_xlim(0, 105)
    ax.set_xlabel("Score de fiabilité / 100", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_ylabel("Ligne", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.xaxis.set_major_locator(mticker.MultipleLocator(20))
    ax.set_title("Priorités de fiabilité par ligne", color=TBM_GRIS_TEXTE, fontsize=10, fontweight="bold")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def risk_scatter_chart(lines: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if lines.empty:
        return None
    fig, ax = plt.subplots(figsize=(6.5, 5))
    _setup_ax(ax)
    for _, row in lines.iterrows():
        c = _score_color(float(row.score), SCORE_SEUILS)
        ax.scatter(float(row.retard_median), float(row.retard_5), c=c, s=30, zorder=3, edgecolors="white", linewidth=0.3)
    for _, row in lines.iterrows():
        ax.text(float(row.retard_median) + max(float(lines.retard_median.max()) * 0.025, 3),
                float(row.retard_5), row.ligne, fontsize=6, color=TBM_GRIS_TEXTE, va="center")
    xmax = float(lines.retard_median.max()) * 1.3 or 120
    ymax = float(lines.retard_5.max()) * 1.3 or 30
    ax.set_xlim(0, xmax)
    ax.set_ylim(0, ymax)
    ax.set_xlabel("Retard médian (secondes)", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_ylabel("Passages > 5 min (%)", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_title("Carte de risque des lignes", color=TBM_GRIS_TEXTE, fontsize=10, fontweight="bold")
    ax.text(0.05, 0.92, "Retards rares mais longs", transform=ax.transAxes, fontsize=7, color=TBM_GRIS_TEXTE + "80", va="top")
    ax.text(0.70, 0.92, "Zone critique", transform=ax.transAxes, fontsize=7, color=TBM_GRIS_TEXTE + "80", va="top")
    ax.text(0.05, 0.05, "Risque faible", transform=ax.transAxes, fontsize=7, color=TBM_GRIS_TEXTE + "80", va="bottom")
    ax.text(0.70, 0.05, "Retards fréquents mais courts", transform=ax.transAxes, fontsize=7, color=TBM_GRIS_TEXTE + "80", va="bottom")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def stop_chart(stop_stats: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if stop_stats.empty:
        return None
    selected = stop_stats.head(12).iloc[::-1].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(7, max(2.5, len(selected) * 0.4)))
    _setup_ax(ax)
    colors = [_score_color(float(r.retard_median), RETARD_SEUILS) for _, r in selected.iterrows()]
    y = range(len(selected))
    ax.barh([i - 0.15 for i in y], selected["retard_moyen"], height=0.25, color=colors, zorder=3, label="Moyen", edgecolor="white", linewidth=0.3, alpha=0.5)
    ax.barh([i + 0.15 for i in y], selected["retard_median"], height=0.25, color=colors, zorder=3, label="Médian", edgecolor="white", linewidth=0.3)
    for i, row in selected.iterrows():
        ax.text(float(row.retard_moyen) + 1.5, i - 0.15, duration(float(row.retard_moyen)), va="center", fontsize=6, color=TBM_GRIS_TEXTE)
        ax.text(float(row.retard_median) + 1.5, i + 0.15, duration(float(row.retard_median)), va="center", fontsize=6, color=TBM_GRIS_TEXTE)
    labels = []
    for _, row in selected.iterrows():
        ligne = row.get("route_short_name") or row.get("main_route", "")
        labels.append(f"{row.stop_name} ({ligne})" if ligne else row.stop_name)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Retard", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_title("Arrêts les plus problématiques du périmètre", color=TBM_GRIS_TEXTE, fontsize=10, fontweight="bold")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def evolution_chart(monthly: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if monthly.empty or len(monthly) < 2:
        return None
    fig, ax = plt.subplots(figsize=(7, 3.5))
    _setup_ax(ax)
    colors = [_score_color(float(r.ponctualite), SCORE_SEUILS) for _, r in monthly.iterrows()]
    for i in range(len(monthly) - 1):
        ax.plot([i, i + 1], [monthly.iloc[i]["ponctualite"], monthly.iloc[i + 1]["ponctualite"]],
                color=TBM_GRIS_TEXTE, linewidth=1.5, zorder=2)
    ax.scatter(range(len(monthly)), monthly["ponctualite"], c=colors, s=40, zorder=3, edgecolors="white", linewidth=0.5)
    ax.set_ylim(50, 100)
    ax.set_xticks(range(len(monthly)))
    ax.set_xticklabels(monthly["mois"].tolist(), fontsize=7, rotation=30, ha="right")
    ax.set_xlabel("Mois", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_ylabel("Ponctualité (%)", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_title("Évolution mensuelle de la ponctualité", color=TBM_GRIS_TEXTE, fontsize=10, fontweight="bold")
    ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def hourly_chart(hourly: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if hourly.empty:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    _setup_ax(ax)
    colors = [_score_color(float(r.retard_5), PCT5_SEUILS) for _, r in hourly.iterrows()]
    ax.bar(hourly["heure"], hourly["retard_5"], color=colors, width=0.7, zorder=3, edgecolor="white", linewidth=0.3)
    for _, row in hourly.iterrows():
        ax.text(int(row.heure), float(row.retard_5) + 0.5, f"{row.retard_5:.1f}",
                ha="center", fontsize=6, color=TBM_GRIS_TEXTE)
    ax.axvspan(7.5, 9.5, color=TBM_GRIS, alpha=0.4, zorder=1)
    ax.axvspan(17.5, 19.5, color=TBM_GRIS, alpha=0.4, zorder=1)
    net_avg = float(hourly["retard_5"].mean())
    ax.axhline(net_avg, color=TBM_BLEU, linewidth=0.8, linestyle="--", zorder=2)
    ax.text(23, net_avg, f"Moyenne réseau : {net_avg:.1f}%", fontsize=6, color=TBM_BLEU, va="bottom", ha="right")
    ax.set_xlim(-0.5, 23.5)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlabel("Heure", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_ylabel("Retards > 5 min (%)", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_title("Risque selon l'heure de départ", color=TBM_GRIS_TEXTE, fontsize=10, fontweight="bold")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def distribution_chart(distribution: pd.DataFrame, output_dir: Path, name: str) -> Path | None:
    if distribution.empty:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    _setup_ax(ax)
    plages = distribution["plage"].tolist()
    dist_color_map = {
        "< -10": TBM_MAGENTA, "-10 a -5": TBM_MAGENTA, "-5 a -2": TBM_MAGENTA,
        "-2 a -1": TBM_ORANGE,
        "-1 a 0": TBM_VERT, "0 a +1": TBM_VERT, "+1 a +2": TBM_VERT,
        "+2 a +5": TBM_ORANGE,
        "+5 a +10": TBM_MAGENTA, "+10 a +20": TBM_MAGENTA, "> +20": TBM_MAGENTA,
    }
    colors = [dist_color_map.get(p, TBM_MAGENTA) for p in plages]
    n = len(distribution)
    ax.bar(range(n), distribution["passages"], color=colors, width=0.7, zorder=3, edgecolor="white", linewidth=0.3)
    for i, (_, row) in enumerate(distribution.iterrows()):
        ax.text(i, int(row.passages) + max(1, int(distribution.passages.max()) * 0.02),
                str(int(row.passages)), ha="center", fontsize=7, color=TBM_GRIS_TEXTE)
    ax.set_xticks(range(n))
    ax.set_xticklabels(plages, fontsize=7, rotation=45, ha="right")
    ax.set_xlabel("Tranche de retard (minutes)", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_ylabel("Nombre de passages", color=TBM_GRIS_TEXTE, fontsize=8)
    ax.set_title("Distribution des retards", color=TBM_GRIS_TEXTE, fontsize=10, fontweight="bold")
    fig.tight_layout(pad=0.8)
    return _save_chart(fig, output_dir, name)


def graphical_annex(lines: pd.DataFrame, scheduled: pd.DataFrame,
                     network_lines: pd.DataFrame | None = None,
                     stop_stats: pd.DataFrame | None = None,
                     monthly_evolution: pd.DataFrame | None = None,
                     output_dir: Path | None = None) -> str:
    if output_dir is None:
        output_dir = Path("/tmp/vigie_charts")
    hourly, distribution = operational_views(scheduled)
    imgs: list[str] = []
    imgs.append(r"\newpage\section*{Annexe — Analyse graphique}")
    imgs.append(r"\small\textbf{Seuils de couleur utilisés dans les graphiques :} "
                r"Vert = bonne performance ($\geq$ 80/100 pour le score de fiabilité, "
                r"$\leq$ 60 s pour le retard moyen/médian, $\leq$ 5\% pour les retards $>$ 5 min), "
                r"Orange = performance moyenne, "
                r"Rouge = performance dégradée. Consulter la section Méthode pour le détail des calculs.\\[.3cm]")
    p = reliability_chart(lines, output_dir, "reliability", network_lines)
    if p:
        imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    p = risk_scatter_chart(lines, output_dir, "risk_scatter")
    if p:
        imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    if stop_stats is not None and not stop_stats.empty:
        p = stop_chart(stop_stats, output_dir, "stops")
        if p:
            imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    if monthly_evolution is not None and len(monthly_evolution) >= 2:
        p = evolution_chart(monthly_evolution, output_dir, "evolution")
        if p:
            imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    imgs.append(r"\newpage\section*{Annexe — Profil opérationnel}")
    p = hourly_chart(hourly, output_dir, "hourly") if not hourly.empty else None
    imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}" if p
                else r"\textit{Aucune heure de départ exploitable pour cette période.}")
    p = distribution_chart(distribution, output_dir, "distribution")
    if p:
        imgs.append(r"\begin{center}\includegraphics[width=\textwidth]{" + str(p) + r"}\end{center}")
    return "\n".join(imgs)


def build_no_data_latex(month: str, scope: Scope, collected_at: str) -> str:
    """Produce a transparent report even when a small municipality has no passage."""
    report_date = datetime.strptime(month, "%Y-%m")
    report_month = f"{FRENCH_MONTHS[report_date.month - 1]} {report_date.year}"
    return rf"""\documentclass[10pt,a4paper]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage[french]{{babel}}
\usepackage[margin=2cm]{{geometry}}
\usepackage{{xcolor,fancyhdr}}
\definecolor{{vigieblue}}{{HTML}}{{009EE3}}
\pagestyle{{fancy}}\fancyhf{{}}\lhead{{\textcolor{{vigieblue}}{{VIGIE TBM}}}}\rhead{{Rapport mensuel}}\cfoot{{\thepage}}
\begin{{document}}
\begin{{center}}
{{\LARGE\bfseries Rapport mensuel de fiabilité des transports TBM}}\\[5pt]
{{\large {latex(report_month).capitalize()} — Destinataire : {latex(scope.recipient)}}}\\[3pt]
\small Périmètre : {latex(scope.description)}\\[2pt]
\small Rapport produit par Elias Khallouk --- eliaskhallouk@gmail.com
\end{{center}}
\vspace{{1cm}}\hrule\vspace{{1cm}}
\section*{{Absence de données exploitables}}
Aucun passage programmé avec une heure de départ et un retard stabilisé n'a été observé dans ce périmètre durant le mois analysé. Il n'est donc pas possible de calculer des indicateurs ni de produire des graphiques fiables pour cette édition.

\vspace{{.4cm}}
Cette absence ne signifie pas nécessairement l'absence de desserte : elle peut résulter d'une couverture de collecte insuffisante, d'une période sans circulation, ou d'arrêts présents dans le GTFS mais non observés dans le flux temps réel.

\vfill
\small\color{{gray}} Source : flux GTFS-RT TripUpdates TBM, données arrêtées au {latex(collected_at)}. Le périmètre repose sur les arrêts géolocalisés dans la commune.
\end{{document}}
"""


def build_latex(month: str, scope: Scope, metrics: dict[str, float | int], change: dict[str, str],
                lines: pd.DataFrame, scheduled: pd.DataFrame, collected_at: str,
                output_dir: Path,
                network_metrics: dict | None = None,
                network_lines: pd.DataFrame | None = None,
                stop_stats: pd.DataFrame | None = None,
                monthly_evolution: pd.DataFrame | None = None,
                gaps: dict | None = None,
                alerts: list[dict] | None = None) -> str:
    report_date = datetime.strptime(month, "%Y-%m")
    report_month = f"{FRENCH_MONTHS[report_date.month - 1]} {report_date.year}"
    worst = lines.head(3)

    # Compute network-wide ranking for alert lines
    net_rank: dict[str, int] = {}
    net_total = 0
    if network_lines is not None and not network_lines.empty:
        ranked = network_lines.sort_values("score", ascending=True).reset_index(drop=True)
        net_total = len(ranked)
        net_rank = {row.route_id: idx + 1 for idx, row in ranked.iterrows()}

    # Collection gaps info for methodology
    if gaps and gaps["gap_seconds"] > 0:
        gap_minutes = gaps["gap_seconds"] // 60
        excluded = gaps["total_raw"] - int(metrics["passages"])
        gap_line = f"{excluded} observations exclues sur {gaps['total_raw']} ({gap_minutes}~min d\'interruption de collecte)."
    else:
        gap_line = "Aucune interruption de collecte significative sur la période."
    evolution_note = rf"\textbf{{Évolution mensuelle.}} {change.get('fiability', '')}"


    alert_routes = {a["route_id"] for a in (alerts or []) if a["route_id"]}

    priority_alerts = "\n".join(
        rf"\item \alertmark{{}} \textbf{{Ligne {latex(row.ligne)}}}"
        + (f" (rang réseau : {net_rank.get(row.route_id, '—')}/{net_total})" if net_rank else "")
        + f" : score {row.score:.1f}/100, {pct(row.retard_5)} de retards supérieurs à 5 minutes, {pct(row.arrets_sautes, 2)} d'arrêts sautés."
        for row in worst.itertuples()
    )

    # Format service alerts for "Perturbations en cours"
    if alerts:
        alert_items = []
        seen = set()
        for a in alerts:
            key = (a["alert_id"], a["route_id"])
            if key in seen:
                continue
            seen.add(key)
            start = datetime.fromtimestamp(a["active_period_start"]).strftime("%d/%m")
            period = f"début {start} (en cours)"
            if a["active_period_end"]:
                end = datetime.fromtimestamp(a["active_period_end"]).strftime("%d/%m")
                period = f"du {start} au {end}"
            ligne = a["route_id"] if a["route_id"] else "Réseau"
            header = a["header_text"] or "(information non disponible)"
            alert_items.append(
                rf"\item \textbf{{Ligne {latex(ligne)}}} : {latex(header)} ({period})"
            )
        perturbations = (
            "\n\\vspace{.35cm}\n"
            "\\textbf{Perturbations en cours}\n"
            "\\begin{itemize}[leftmargin=1.4em,itemsep=.25em]\n"
            + "\n".join(alert_items)
            + "\n\\end{itemize}"
        )
    else:
        perturbations = ""

    return rf"""\documentclass[10pt,a4paper]{{article}}
\usepackage[french]{{babel}}
\usepackage{{fontspec}}
\setmainfont{{Lato}}
\usepackage[margin=1.7cm]{{geometry}}
\usepackage{{amsmath,booktabs,longtable,array,xcolor,tabularx,enumitem,graphicx,tcolorbox}}
\usepackage{{fancyhdr}}
\definecolor{{vigiebleu}}{{HTML}}{{009EE3}}
\definecolor{{vigielight}}{{HTML}}{{FFFFFF}}
\definecolor{{alert}}{{HTML}}{{E7007C}}
\pagestyle{{fancy}}\fancyhf{{}}\lhead{{\textcolor{{vigiebleu}}{{VIGIE TBM}}}}\rhead{{Rapport mensuel}}\cfoot{{\thepage}}
\setlength{{\parindent}}{{0pt}}
\newcommand{{\kpi}}[3][vigiebleu]{{\begin{{tcolorbox}}[width=.28\textwidth,sharp corners,boxrule=0pt,leftrule=3pt,colback=vigielight,colframe=#1,arc=0pt,outer arc=0pt,left=6pt,right=4pt,top=4pt,bottom=4pt,halign=flush left,valign=top]{{\scriptsize #2\\[3pt]}}{{\Large\bfseries\color{{#1}} #3}}\end{{tcolorbox}}}}
\newcommand{{\alertmark}}{{\textcolor{{alert}}{{⚠}}}}

\begin{{document}}
\begin{{center}}
{{\LARGE\bfseries Rapport mensuel de fiabilité des transports TBM}}\\[5pt]
{{\large {latex(report_month).capitalize()} — Destinataire : {latex(scope.recipient)}}}\\[3pt]
\small Périmètre : {latex(scope.description)}\\[2pt]
\small Rapport produit par Elias Khallouk --- eliaskhallouk@gmail.com
\end{{center}}
\vspace{{.45cm}}
\hrule\vspace{{.45cm}}
\section*{{Synthèse exécutive}}
\textit{{Cette page présente les indicateurs à retenir. Les résultats détaillés et la méthode figurent en annexe.}}\\[.5cm]
\makebox[\textwidth]{{\kpi[{kpi_color(metrics, 'fiability')}]{{Fiabilité}}{{{number(int(metrics['fiability']))}{net_val(network_metrics, 'fiability', number)}}}\hfill
\kpi{{Passages analysés}}{{{number(int(metrics['passages']))}{net_val(network_metrics, 'passages', number)}}}\hfill
\kpi[{kpi_color(metrics, 'ponctualite')}]{{Ponctualité (retard $\leq$ 5 min)}}{{{pct(float(metrics['ponctualite']))}{net_val(network_metrics, 'ponctualite', pct)}}}}}

\vspace{{1cm}}
\makebox[\textwidth]{{\kpi[{kpi_color(metrics, 'retard')}]{{Retard moyen}}{{{duration(float(metrics['retard']))}{net_val(network_metrics, 'retard', duration)}}}\hfill
\kpi[{kpi_color(metrics, 'retard_median')}]{{Retard médian}}{{{duration(float(metrics['retard_median']))}{net_val(network_metrics, 'retard_median', duration)}}}\hfill
\kpi[{kpi_color(metrics, 'skip_rate')}]{{Arrêts sautés}}{{{pct(float(metrics['skip_rate']), 2)}{net_val(network_metrics, 'skip_rate', lambda v: pct(v, 2))}}}}}

\vspace{{.7cm}}
\begin{{tabularx}}{{\textwidth}}{{@{{}}lXXXX@{{}}}}
\toprule
 & \textbf{{Fiabilité}} & \textbf{{Ponctualité}} & \textbf{{Retards moyen / médian}} & \textbf{{Arrêts sautés}} \\
\midrule
\textbf{{Évolution}} & {latex(change['fiability'])} & {latex(change['ponctualite'])} & {latex(change['retard'])} & {latex(change['skip_rate'])} \\
\bottomrule
\end{{tabularx}}

\vspace{{.5cm}}
\textbf{{Lecture du mois.}} {executive_message(metrics, lines, scope)}

\vspace{{.3cm}}
\textbf{{Score de fiabilité.}} Il est calculé ainsi : \textit{{score = max(0 ; ponctualité - 2 x taux d'arrêts sautés)}}. La ponctualité (part des passages avec au plus cinq minutes de retard) constitue donc la base sur 100 ; chaque point d'arrêts sautés retire deux points. Un score faible signale une ligne prioritaire.

\vspace{{.35cm}}
\textbf{{Alertes prioritaires}}
\begin{{itemize}}[leftmargin=1.4em,itemsep=.25em]
{priority_alerts}
\end{{itemize}}
{perturbations}

\vfill
\small\color{{gray}} Source : flux GTFS-RT TripUpdates TBM, données arrêtées au {latex(collected_at)}. Les vingt dernières minutes du flux sont exclues afin de ne considérer que des observations stabilisées.
\newpage

\section*{{Annexe — Résultats détaillés}}
\textbf{{Périmètre analysé :}} {latex(scope.description)}. Les lignes sont classées de la plus à la moins prioritaire selon un score combinant la ponctualité et les arrêts sautés.\\[.4cm]
\renewcommand{{\arraystretch}}{{1.18}}
\begin{{longtable}}{{lrrrrr}}
\toprule
\textbf{{Ligne}} & \textbf{{Passages}} & \textbf{{À l'heure}} & \textbf{{Retards moy. / méd.}} & \textbf{{> 5 min}} & \textbf{{Arrêts sautés}} \\
\midrule
\endfirsthead
\toprule
\textbf{{Ligne}} & \textbf{{Passages}} & \textbf{{À l'heure}} & \textbf{{Retards moy. / méd.}} & \textbf{{> 5 min}} & \textbf{{Arrêts sautés}} \\
\midrule
\endhead
{line_table(lines, network_lines, alert_routes)}
\bottomrule
\end{{longtable}}

{graphical_annex(lines, scheduled, network_lines, stop_stats, monthly_evolution, output_dir)}

\section*{{Méthode et calcul de la fiabilité}}
L'indice de fiabilité est un score synthétique (de 0 à 100) conçu pour identifier rapidement les lignes de transport qui posent le plus de difficultés aux usagers.

Contrairement à une simple mesure de temps, cet indicateur combine deux facteurs clés :

\begin{{itemize}}[leftmargin=1.4em]
\item \textbf{{La ponctualité (la base)}} : la part des passages effectués avec au maximum 5 minutes de retard. Au-delà de 5 minutes, le retard est jugé trop pénalisant pour l'usager et le trajet fait baisser cette note de base.
\item \textbf{{Les arrêts sautés (la pénalité)}} : lorsqu'un véhicule ne dessert pas un arrêt prévu (événement \texttt{{SKIPPED}}), la gêne est maximale. Chaque pourcent d'arrêts sautés retire donc 2 points au score global.
\end{{itemize}}

\[
\text{{Score de fiabilité}} = \max(0 \;,\; \text{{Ponctualité}} - 2 \times \text{{Taux d'arrêts sautés}})
\]

\vspace{{.2cm}}
À noter~:
\begin{{itemize}}[leftmargin=1.4em]
\item Un score faible indique une ligne prioritaire à corriger.
\item Ce rapport repose sur l'analyse des données GTFS-RT consolidées (observations sorties du flux depuis plus de 20 minutes) et permet de mesurer la qualité de service observée, sans en analyser les causes opérationnelles.
\end{{itemize}}

\subsection*{{Précision et limites}}
\textbf{{Marge d'incertitude.}} Les retards sont calculés à partir de l'heure de départ effective transmise par le véhicule dans le flux GTFS-RT. Ce flux est interrogé toutes les 60~secondes~; l'heure réelle de départ peut donc précéder ou suivre l'observation d'au plus 60~secondes. Cette marge d'incertitude ($\pm 60$~s) est inhérente au dispositif de collecte et ne remet pas en cause la pertinence des tendances présentées.

\textbf{{Trous de collecte.}} Lorsque le service de collecte est interrompu (redémarrage, indisponibilité réseau), les données produites pendant l'intervalle sont exclues de l'analyse.
{gap_line}
{evolution_note}

\textbf{{Alertes travaux.}} Les alertes affichées dans la synthèse exécutive (\alertmark) sont issues du flux ServiceAlerts TBM et sont reproduites à titre indicatif. Elles ne sont pas utilisées pour filtrer ou corriger les indicateurs de ponctualité. La présence d'une alerte sur une ligne ne signifie pas que les retards ou arrêts sautés observés sont causés par les travaux annoncés.

\vspace{{.4cm}}
\hrule\vspace{{.3cm}}
\small Elias Khallouk --- eliaskhallouk@gmail.com \hfill Vigie-TBM --- {latex(collected_at)}
\end{{document}}
"""


def compile_pdf(tex_path: Path) -> Path:
    executable = shutil.which("xelatex") or shutil.which("lualatex")
    if not executable:
        raise RuntimeError("xelatex/lualatex introuvable. Le fichier .tex a été généré, mais ne peut pas être compilé en PDF.")
    command = [executable, "-interaction=nonstopmode", "-halt-on-error", tex_path.name]
    for _ in range(2):
        result = subprocess.run(command, cwd=tex_path.parent, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(f"La compilation LaTeX a échoué :\n{result.stdout[-2000:]}\n{result.stderr[-1000:]}")
    pdf_path = tex_path.with_suffix(".pdf")
    # Clean up auxiliary files
    for suffix in (".aux", ".log"):
        aux = tex_path.with_suffix(suffix)
        if aux.exists():
            aux.unlink()
    return pdf_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère le rapport mensuel Vigie TBM (LaTeX/PDF).")
    parser.add_argument("--month", help="Mois analysé au format AAAA-MM (par défaut : dernier mois disponible).")
    parser.add_argument("--db-path", default=DEFAULT_DB, type=Path, help="Base SQLite à analyser.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT, type=Path, help="Répertoire des rapports générés.")
    parser.add_argument("--recipient", help="Destinataire affiché dans le rapport réseau.")
    parser.add_argument("--routes", help="Liste de route_id séparés par des virgules (filtre complémentaire facultatif).")
    parser.add_argument("--communes", help="Communes séparées par des virgules : le rapport est alors filtré sur leurs arrêts.")
    parser.add_argument("--profile", help="Identifiant d'un destinataire dans le fichier de profils.")
    parser.add_argument("--recipients-file", default=PROJECT_ROOT / "reports" / "recipients.json", help="Fichier JSON de profils territoriaux.")
    parser.add_argument("--compile", action="store_true", help="Compile aussi le .tex en PDF avec pdflatex.")
    args = parser.parse_args()
    if not args.db_path.exists():
        parser.error(f"Base introuvable : {args.db_path}")
    try:
        scope = load_scope(args)
        with sqlite3.connect(args.db_path) as conn:
            month = resolve_month(conn, args.month)
            scheduled, skipped, collected_at = query_observations(conn, month, scope)
            if scheduled.empty:
                content = build_no_data_latex(month, scope, collected_at)
                network_metrics = None
                network_lines = None
                stop_stats = None
            else:
                lines = make_line_stats(scheduled, skipped)
                current = kpis(scheduled, skipped)
                previous_scheduled, previous_skipped, _ = query_observations(conn, previous_month(month), scope)
                previous = kpis(previous_scheduled, previous_skipped) if not previous_scheduled.empty else None
                network_metrics = None
                network_lines = None
                stop_stats = None
                monthly_evolution = None
                if scope.communes:
                    net_scope = Scope("Réseau TBM", scope.routes, [], "Réseau TBM global")
                    net_scheduled, net_skipped, _ = query_observations(conn, month, net_scope)
                    if not net_scheduled.empty:
                        network_metrics = kpis(net_scheduled, net_skipped)
                        network_lines = make_line_stats(net_scheduled, net_skipped)
                    stop_stats = query_stop_stats(conn, month, scope)
                monthly_evolution = query_monthly_evolution(conn, month, scope)
                gaps = query_collection_gaps(conn, month)
                route_ids = set(scheduled["route_id"].unique()) if not scheduled.empty else set()
                alerts_data = query_service_alerts(conn, month, route_ids) if route_ids else []
                content = build_latex(month, scope, current, comparison(current, previous),
                                      lines, scheduled, collected_at,
                                      args.output_dir,
                                      network_metrics, network_lines, stop_stats,
                                      monthly_evolution, gaps, alerts_data)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        tex_path = args.output_dir / f"vigie-tbm-{month}-{safe_slug(scope.recipient)}.tex"
        tex_path.write_text(content, encoding="utf-8")
        if args.compile:
            pdf_path = compile_pdf(tex_path)
            tex_path.unlink(missing_ok=True)
            print(f"PDF généré : {pdf_path}")
        else:
            print(f"Rapport LaTeX généré : {tex_path}")
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
