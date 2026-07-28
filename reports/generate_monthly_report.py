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
from datetime import datetime
from pathlib import Path

import pandas as pd

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
    """Return a LaTeX color name based on the metric value (good→green, medium→orange, bad→red)."""
    if key == "ponctualite":
        v = metrics[key]
        return "vigiegreen" if v >= 90 else "vigieorange" if v >= 80 else "alert"
    if key in ("retard", "retard_median"):
        v = abs(metrics[key])
        return "vigiegreen" if v <= 60 else "vigieorange" if v <= 120 else "alert"
    if key == "skip_rate":
        v = metrics[key]
        return "vigiegreen" if v <= 1 else "vigieorange" if v <= 5 else "alert"
    return "vigieblue"


def net_val(network_metrics: dict | None, key: str, formatter) -> str:
    """Return a LaTeX snippet showing the network-wide comparison value, or empty."""
    if network_metrics is None:
        return ""
    return f"\\\\ {{\\tiny\\color{{gray}}Réseau: {formatter(network_metrics[key])}}}"


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
          AND strftime('%Y-%m', datetime(o.departure_time, 'unixepoch', 'localtime')) = ?
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
    """Return per-stop delay statistics for the given scope."""
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
        SELECT o.stop_id, COALESCE(s.stop_name, o.stop_id) AS stop_name, o.departure_delay
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
    ).sort_values("retard_median", ascending=False)
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
    }


def comparison(current: dict[str, float | int], previous: dict[str, float | int] | None) -> dict[str, str]:
    if not previous:
        return {"ponctualite": "Pas de comparaison", "retard": "Pas de comparaison", "skip_rate": "Pas de comparaison"}
    return {
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


def line_table(lines: pd.DataFrame, network_lines: pd.DataFrame | None = None) -> str:
    net_lookup: dict[str, tuple] = {}
    if network_lines is not None and not network_lines.empty:
        net_lookup = {row.route_id: row for row in network_lines.itertuples()}
    table_rows = []
    for row in lines.itertuples():
        net_row = net_lookup.get(row.route_id)
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
            f"{latex(row.ligne)} & {passages_cell} & {ponctualite_cell} & "
            f"{duration(row.retard_moyen)} / {duration(row.retard_median)} & {retard_5_cell} & {arrets_cell} \\\\"
        )
    return "\n".join(table_rows)


def pgf_number(value: float) -> str:
    """PGFPlots expects a decimal point, unlike textual numbers in the report."""
    return f"{float(value):.2f}"


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
    bins = [-3600, -600, -300, -120, -60, 0, 60, 120, 300, 600, 1200, 3601]
    labels = ["< -10", "-10 a -5", "-5 a -2", "-2 a -1", "-1 a 0", "0 a +1", "+1 a +2", "+2 a +5", "+5 a +10", "+10 a +20", "> +20"]
    classes = pd.cut(dated["departure_delay"].clip(-3600, 3600), bins=bins, labels=labels, right=False)
    distribution = classes.value_counts(sort=False).rename_axis("plage").reset_index(name="passages")
    return hourly, distribution


def reliability_chart(lines: pd.DataFrame, network_lines: pd.DataFrame | None = None) -> str:
    selected = lines.head(15).reset_index(drop=True)
    if selected.empty:
        return ""
    ticks = ",".join(str(index) for index in selected.index)
    labels = ",".join(latex(value) for value in selected["ligne"])
    coordinates = " ".join(f"({pgf_number(row.score)},{index})" for index, row in selected.iterrows())
    height = max(5.0, len(selected) * 0.42)

    net_coordinates = ""
    show_legend = False
    if network_lines is not None and not network_lines.empty:
        net_scores = []
        for index, row in selected.iterrows():
            net_row = network_lines[network_lines["route_id"] == row.route_id]
            if not net_row.empty:
                net_scores.append((pgf_number(float(net_row.iloc[0]["score"])), index))
        if net_scores:
            net_coordinates = " ".join(f"({score},{index})" for score, index in net_scores)
            show_legend = True

    legend_block = r"\legend{Périmètre, Réseau TBM}" if show_legend else ""
    bar_width = 7
    bar_shift = 3.5
    if show_legend:
        bar_width = 5
        bar_shift = 2.5
    net_shift = -bar_shift
    net_plot_block = (
        rf"\addplot[fill=gray!35, draw=gray!60!black, bar shift={net_shift}pt] coordinates {{{net_coordinates}}};"
        if net_coordinates else ""
    )

    return rf"""
\subsection*{{Priorités de fiabilité par ligne}}
\textit{{Les quinze lignes au score le plus faible sont présentées. Plus le score est bas, plus la priorité de suivi est élevée.}}\\[.2cm]
\begin{{center}}
\begin{{tikzpicture}}
\begin{{axis}}[xbar, width=.94\textwidth, height={height:.1f}cm, xmin=0, xmax=100,
  xlabel={{Score de fiabilité / 100}},
  ytick={{{ticks}}}, yticklabels={{{labels}}},
  y dir=reverse, grid=major, grid style={{gray!20}}, bar width={bar_width}pt,
  legend style={{at={{(0.5,-0.12)}}, anchor=north, legend columns=-1, font=\footnotesize}}]
{net_plot_block}
\addplot[fill=vigieblue!82, draw=vigieblue, bar shift={bar_shift}pt] coordinates {{{coordinates}}};
{legend_block}
\end{{axis}}
\end{{tikzpicture}}
\end{{center}}
"""


def stop_chart(stop_stats: pd.DataFrame) -> str:
    if stop_stats.empty:
        return ""
    selected = stop_stats.head(12).reset_index(drop=True)
    ticks = ",".join(str(index) for index in selected.index)
    labels = ",".join(latex(value) for value in selected["stop_name"])
    coords_moyen = " ".join(f"({pgf_number(row.retard_moyen)},{index})" for index, row in selected.iterrows())
    coords_median = " ".join(f"({pgf_number(row.retard_median)},{index})" for index, row in selected.iterrows())
    height = max(4.0, len(selected) * 0.50)
    worst = stop_stats.iloc[0]
    return rf"""
\subsection*{{Arrêts les plus problématiques du périmètre}}
\textit{{Les douze arrêts ayant le retard médian le plus élevé. L'arrêt {latex(worst.stop_name)} ({duration(float(worst.retard_moyen))} en moyenne, {duration(float(worst.retard_median))} en médiane) est le plus critique.}}\\[.2cm]
\begin{{center}}
\begin{{tikzpicture}}
\begin{{axis}}[xbar, width=.94\textwidth, height={height:.1f}cm,
  xlabel={{Retard (secondes)}},
  ytick={{{ticks}}}, yticklabels={{{labels}}},
  y dir=reverse, grid=major, grid style={{gray!20}}, bar width=5pt,
  legend style={{at={{(0.5,-0.10)}}, anchor=north, legend columns=-1, font=\footnotesize}}]
\addplot[fill=vigieblue!70, draw=vigieblue, bar shift=-2.5pt] coordinates {{{coords_moyen}}};
\addplot[fill=vigieorange!85, draw=vigieorange!90!black, bar shift=2.5pt] coordinates {{{coords_median}}};
\legend{{Moyen, Médian}}
\end{{axis}}
\end{{tikzpicture}}
\end{{center}}
"""


def evolution_chart(monthly: pd.DataFrame) -> str:
    if monthly.empty or len(monthly) < 2:
        return ""
    ticks = ",".join(str(index) for index in range(len(monthly)))
    labels = ",".join(latex(row.mois) for row in monthly.itertuples())
    coords_ponctualite = " ".join(f"({index},{pgf_number(row.ponctualite)})" for index, row in monthly.iterrows())
    return rf"""
\subsection*{{Évolution mensuelle de la ponctualité}}
\textit{{Taux de passages à l'heure (retard $\leq$ 5 min) sur le périmètre, mois par mois.}}\\[.2cm]
\begin{{center}}
\begin{{tikzpicture}}
\begin{{axis}}[width=.92\textwidth, height=6cm, xlabel={{Mois}},
  ylabel={{Ponctualité (\%)}}, ymin=50, ymax=100,
  xtick={{{ticks}}}, xticklabels={{{labels}}},
  grid=major, grid style={{gray!20}},
  legend style={{at={{(0.5,-0.12)}}, anchor=north, legend columns=-1, font=\footnotesize}}]
\addplot[color=vigieblue, very thick, mark=*, mark size=2.5pt] coordinates {{{coords_ponctualite}}};
\legend{{Ponctualité}}
\end{{axis}}
\end{{tikzpicture}}
\end{{center}}
"""


def risk_scatter_chart(lines: pd.DataFrame) -> str:
    if lines.empty:
        return ""
    coordinates = " ".join(f"({pgf_number(row.retard_moyen)},{pgf_number(row.retard_5)})" for row in lines.itertuples())
    return rf"""
\subsection*{{Carte de risque des lignes}}
\textit{{Chaque point représente une ligne : plus il est à droite et en haut, plus son retard moyen et sa part de retards supérieurs à cinq minutes sont élevés.}}\\[.2cm]
\begin{{center}}
\begin{{tikzpicture}}
\begin{{axis}}[width=.87\textwidth, height=7cm, xlabel={{Retard moyen (secondes)}},
  ylabel={{Passages avec retard > 5 min (\%)}}, grid=major, grid style={{gray!20}}]
\addplot[only marks, mark=*, mark size=2.2pt, color=vigieblue!80] coordinates {{{coordinates}}};
\end{{axis}}
\end{{tikzpicture}}
\end{{center}}
"""


def hourly_chart(hourly: pd.DataFrame) -> str:
    if hourly.empty:
        return ""
    coordinates = " ".join(f"({int(row.heure)},{pgf_number(row.retard_5)})" for row in hourly.itertuples())
    return rf"""
\subsection*{{Risque selon l'heure de départ}}
\textit{{Part des passages accusant plus de cinq minutes de retard, selon l'heure locale de départ.}}\\[.2cm]
\begin{{center}}
\begin{{tikzpicture}}
\begin{{axis}}[ybar, width=.88\textwidth, height=6.5cm, ymin=0, xlabel={{Heure locale}},
  ylabel={{Retards > 5 min (\%)}}, xtick=data, grid=major, grid style={{gray!20}}, bar width=8pt]
\addplot[fill=vigiegreen!85, draw=vigiegreen!90!black] coordinates {{{coordinates}}};
\end{{axis}}
\end{{tikzpicture}}
\end{{center}}
"""


def distribution_chart(distribution: pd.DataFrame) -> str:
    if distribution.empty:
        return ""
    ticks = ",".join(str(index) for index in distribution.index)
    labels = ",".join(latex(value) for value in distribution["plage"])
    coordinates = " ".join(f"({index},{int(row.passages)})" for index, row in distribution.iterrows())
    return rf"""
\subsection*{{Distribution des écarts à l'horaire théorique}}
\textit{{Nombre de passages par classe de retard ou d'avance.}}\\[.2cm]
\begin{{center}}
\begin{{tikzpicture}}
\begin{{axis}}[ybar, width=.98\textwidth, height=6.5cm, xlabel={{Écart au départ théorique (minutes)}},
  ylabel={{Nombre de passages}}, xtick={{{ticks}}}, xticklabels={{{labels}}},
  x tick label style={{rotate=35, anchor=east}}, grid=major, grid style={{gray!20}}, bar width=8pt]
\addplot[fill=vigieorange!84, draw=vigieorange!90!black] coordinates {{{coordinates}}};
\end{{axis}}
\end{{tikzpicture}}
\end{{center}}
"""


def graphical_annex(lines: pd.DataFrame, scheduled: pd.DataFrame,
                     network_lines: pd.DataFrame | None = None,
                     stop_stats: pd.DataFrame | None = None,
                     monthly_evolution: pd.DataFrame | None = None) -> str:
    hourly, distribution = operational_views(scheduled)
    parts = [
        r"\newpage\section*{Annexe — Analyse graphique}",
        reliability_chart(lines, network_lines),
        risk_scatter_chart(lines),
    ]
    if stop_stats is not None and not stop_stats.empty:
        parts.append(stop_chart(stop_stats))
    evo = evolution_chart(monthly_evolution) if monthly_evolution is not None else ""
    if evo:
        parts.append(evo)
    parts.extend([
        r"\newpage\section*{Annexe — Profil opérationnel}",
        hourly_chart(hourly) or r"\textit{Aucune heure de départ exploitable pour cette période.}",
        distribution_chart(distribution),
    ])
    return "\n".join(parts)


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
\definecolor{{vigieblue}}{{HTML}}{{083B73}}
\pagestyle{{fancy}}\fancyhf{{}}\lhead{{\textcolor{{vigieblue}}{{VIGIE TBM}}}}\rhead{{Rapport mensuel}}\cfoot{{\thepage}}
\begin{{document}}
\begin{{center}}
{{\LARGE\bfseries Rapport mensuel de fiabilité des transports TBM}}\\[5pt]
{{\large {latex(report_month).capitalize()} — Destinataire : {latex(scope.recipient)}}}\\[3pt]
\small Périmètre : {latex(scope.description)}\\[2pt]
\small Rapport produit par KHALLOUK Elias
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
                network_metrics: dict | None = None,
                network_lines: pd.DataFrame | None = None,
                stop_stats: pd.DataFrame | None = None,
                monthly_evolution: pd.DataFrame | None = None) -> str:
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

    alerts = "\n".join(
        f"\\item \\textbf{{Ligne {latex(row.ligne)}}}"
        + (f" (rang réseau : {net_rank.get(row.route_id, '—')}/{net_total})" if net_rank else "")
        + f" : score {row.score:.1f}/100, {pct(row.retard_5)} de retards supérieurs à 5 minutes, {pct(row.arrets_sautes, 2)} d'arrêts sautés."
        for row in worst.itertuples()
    )

    return rf"""\documentclass[10pt,a4paper]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage[french]{{babel}}
\usepackage[margin=1.7cm]{{geometry}}
\usepackage{{amsmath,booktabs,longtable,array,xcolor,tabularx,enumitem,pgfplots}}
\usepackage{{fancyhdr}}
\definecolor{{vigieblue}}{{HTML}}{{083B73}}
\definecolor{{vigielight}}{{HTML}}{{EAF3FC}}
\definecolor{{alert}}{{HTML}}{{A52A2A}}
\definecolor{{vigiegreen}}{{HTML}}{{158C72}}
\definecolor{{vigieorange}}{{HTML}}{{D38419}}
\pgfplotsset{{compat=1.18}}
\pagestyle{{fancy}}\fancyhf{{}}\lhead{{\textcolor{{vigieblue}}{{VIGIE TBM}}}}\rhead{{Rapport mensuel}}\cfoot{{\thepage}}
\setlength{{\parindent}}{{0pt}}
\newcommand{{\kpi}}[3][vigieblue]{{\begin{{minipage}}[t]{{.30\textwidth}}\raggedright\colorbox{{vigielight}}{{\parbox{{.91\textwidth}}{{\scriptsize #2\\[3pt]\textcolor{{#1}}{{\Large\bfseries #3}}}}}}\end{{minipage}}}}
\begin{{document}}
\begin{{center}}
{{\LARGE\bfseries Rapport mensuel de fiabilité des transports TBM}}\\[5pt]
{{\large {latex(report_month).capitalize()} — Destinataire : {latex(scope.recipient)}}}\\[3pt]
\small Périmètre : {latex(scope.description)}\\[2pt]
\small Rapport produit par KHALLOUK Elias
\end{{center}}
\vspace{{.45cm}}
\hrule\vspace{{.45cm}}
\section*{{Synthèse exécutive}}
\textit{{Cette page présente les indicateurs à retenir. Les résultats détaillés et la méthode figurent en annexe.}}\\[.5cm]
\kpi{{Passages analysés}}{{{number(int(metrics['passages']))}{net_val(network_metrics, 'passages', number)}}}\hfill
\kpi[{kpi_color(metrics, 'ponctualite')}]{{Ponctualité (retard $\leq$ 5 min)}}{{{pct(float(metrics['ponctualite']))}{net_val(network_metrics, 'ponctualite', pct)}}}\hfill
\kpi[{kpi_color(metrics, 'retard')}]{{Retard moyen}}{{{duration(float(metrics['retard']))}{net_val(network_metrics, 'retard', duration)}}}\\[.35cm]
\hspace{{.17\textwidth}}\kpi[{kpi_color(metrics, 'retard_median')}]{{Retard médian}}{{{duration(float(metrics['retard_median']))}{net_val(network_metrics, 'retard_median', duration)}}}\hfill
\kpi[{kpi_color(metrics, 'skip_rate')}]{{Arrêts sautés}}{{{pct(float(metrics['skip_rate']), 2)}{net_val(network_metrics, 'skip_rate', lambda v: pct(v, 2))}}}

\vspace{{.7cm}}
\begin{{tabularx}}{{\textwidth}}{{@{{}}lXXX@{{}}}}
\toprule
 & \textbf{{Ponctualité}} & \textbf{{Retards moyen / médian}} & \textbf{{Arrêts sautés}} \\
\midrule
\textbf{{Évolution}} & {latex(change['ponctualite'])} & {latex(change['retard'])} & {latex(change['skip_rate'])} \\
\bottomrule
\end{{tabularx}}

\vspace{{.5cm}}
\textbf{{Lecture du mois.}} {executive_message(metrics, lines, scope)}

\vspace{{.3cm}}
\textbf{{Score de fiabilité.}} Il est calculé ainsi : \textit{{score = max(0 ; ponctualité - 2 x taux d'arrêts sautés)}}. La ponctualité (part des passages avec au plus cinq minutes de retard) constitue donc la base sur 100 ; chaque point d'arrêts sautés retire deux points. Un score faible signale une ligne prioritaire.

\vspace{{.35cm}}
\textbf{{Alertes prioritaires}}
\begin{{itemize}}[leftmargin=1.4em,itemsep=.25em]
{alerts}
\end{{itemize}}

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
{line_table(lines, network_lines)}
\bottomrule
\end{{longtable}}

{graphical_annex(lines, scheduled, network_lines, stop_stats, monthly_evolution)}

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
\end{{document}}
"""


def compile_pdf(tex_path: Path) -> Path:
    executable = shutil.which("pdflatex")
    if not executable:
        raise RuntimeError("pdflatex est introuvable. Le fichier .tex a été généré, mais ne peut pas être compilé en PDF.")
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
                content = build_latex(month, scope, current, comparison(current, previous),
                                      lines, scheduled, collected_at,
                                      network_metrics, network_lines, stop_stats,
                                      monthly_evolution)
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
