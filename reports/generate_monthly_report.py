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


def executive_message(metrics: dict[str, float | int], lines: pd.DataFrame) -> str:
    worst = lines.iloc[0]
    if metrics["ponctualite"] >= 90:
        assessment = "Le niveau de ponctualité observé est globalement satisfaisant."
    elif metrics["ponctualite"] >= 80:
        assessment = "Le réseau présente une fiabilité intermédiaire qui appelle un suivi ciblé."
    else:
        assessment = "Le niveau de ponctualité observé appelle une attention prioritaire."
    return (
        f"{assessment} La principale alerte concerne la ligne {latex(worst.ligne)}, avec un score de fiabilité "
        f"de {worst.score:.1f}/100 et {worst.retard_5:.1f}\\% de passages au-delà de cinq minutes de retard."
    )


def line_table(lines: pd.DataFrame) -> str:
    table_rows = []
    for row in lines.itertuples():
        table_rows.append(
            f"{latex(row.ligne)} & {number(int(row.passages))} & {pct(row.ponctualite)} & "
            f"{duration(row.retard_moyen)} / {duration(row.retard_median)} & {pct(row.retard_5)} & {pct(row.arrets_sautes, 2)} \\\\"
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


def reliability_chart(lines: pd.DataFrame) -> str:
    selected = lines.head(15).reset_index(drop=True)
    if selected.empty:
        return ""
    ticks = ",".join(str(index) for index in selected.index)
    labels = ",".join(latex(value) for value in selected["ligne"])
    coordinates = " ".join(f"({pgf_number(row.score)},{index})" for index, row in selected.iterrows())
    height = max(5.0, len(selected) * 0.42)
    return rf"""
\subsection*{{Priorités de fiabilité par ligne}}
\textit{{Les quinze lignes au score le plus faible sont présentées. Plus le score est bas, plus la priorité de suivi est élevée.}}\\[.2cm]
\begin{{center}}
\begin{{tikzpicture}}
\begin{{axis}}[xbar, width=.94\textwidth, height={height:.1f}cm, xmin=0, xmax=100,
  xlabel={{Score de fiabilité / 100}}, ytick={{{ticks}}}, yticklabels={{{labels}}},
  y dir=reverse, grid=major, grid style={{gray!20}}, bar width=7pt]
\addplot[fill=vigieblue!82, draw=vigieblue] coordinates {{{coordinates}}};
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


def graphical_annex(lines: pd.DataFrame, scheduled: pd.DataFrame) -> str:
    hourly, distribution = operational_views(scheduled)
    return "\n".join([
        r"\newpage\section*{Annexe — Analyse graphique}",
        reliability_chart(lines),
        risk_scatter_chart(lines),
        r"\newpage\section*{Annexe — Profil opérationnel}",
        hourly_chart(hourly) or r"\textit{Aucune heure de départ exploitable pour cette période.}",
        distribution_chart(distribution),
    ])


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


def build_latex(month: str, scope: Scope, metrics: dict[str, float | int], change: dict[str, str], lines: pd.DataFrame, scheduled: pd.DataFrame, collected_at: str) -> str:
    report_date = datetime.strptime(month, "%Y-%m")
    report_month = f"{FRENCH_MONTHS[report_date.month - 1]} {report_date.year}"
    worst = lines.head(3)
    alerts = "\n".join(
        f"\\item \\textbf{{Ligne {latex(row.ligne)}}} : score {row.score:.1f}/100, {pct(row.retard_5)} de retards supérieurs à 5 minutes, {pct(row.arrets_sautes, 2)} d'arrêts sautés."
        for row in worst.itertuples()
    )
    return rf"""\documentclass[10pt,a4paper]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage[french]{{babel}}
\usepackage[margin=1.7cm]{{geometry}}
\usepackage{{booktabs,longtable,array,xcolor,tabularx,enumitem,pgfplots}}
\usepackage{{fancyhdr}}
\definecolor{{vigieblue}}{{HTML}}{{083B73}}
\definecolor{{vigielight}}{{HTML}}{{EAF3FC}}
\definecolor{{alert}}{{HTML}}{{A52A2A}}
\definecolor{{vigiegreen}}{{HTML}}{{158C72}}
\definecolor{{vigieorange}}{{HTML}}{{D38419}}
\pgfplotsset{{compat=1.18}}
\pagestyle{{fancy}}\fancyhf{{}}\lhead{{\textcolor{{vigieblue}}{{VIGIE TBM}}}}\rhead{{Rapport mensuel}}\cfoot{{\thepage}}
\setlength{{\parindent}}{{0pt}}
\newcommand{{\kpi}}[2]{{\begin{{minipage}}[t]{{.30\textwidth}}\raggedright\colorbox{{vigielight}}{{\parbox{{.91\textwidth}}{{\scriptsize\mbox{{#1}}\\[3pt]\textcolor{{vigieblue}}{{\Large\bfseries #2}}}}}}\end{{minipage}}}}
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
\kpi{{Passages analysés}}{{{number(int(metrics['passages']))}}}\hfill
\kpi{{Ponctualité (retard $\leq$ 5 min)}}{{{pct(float(metrics['ponctualite']))}}}\hfill
\kpi{{Retard moyen}}{{{duration(float(metrics['retard']))}}}\\[.35cm]
\hspace{{.17\textwidth}}\kpi{{Retard médian}}{{{duration(float(metrics['retard_median']))}}}\hfill
\kpi{{Arrêts sautés}}{{{pct(float(metrics['skip_rate']), 2)}}}

\vspace{{.7cm}}
\begin{{tabularx}}{{\textwidth}}{{@{{}}lXXX@{{}}}}
\toprule
 & \textbf{{Ponctualité}} & \textbf{{Retards moyen / médian}} & \textbf{{Arrêts sautés}} \\
\midrule
\textbf{{Évolution}} & {latex(change['ponctualite'])} & {latex(change['retard'])} & {latex(change['skip_rate'])} \\
\bottomrule
\end{{tabularx}}

\vspace{{.5cm}}
\textbf{{Lecture du mois.}} {executive_message(metrics, lines)}

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
{line_table(lines)}
\bottomrule
\end{{longtable}}

{graphical_annex(lines, scheduled)}

\section*{{Méthode et limites}}
\begin{{itemize}}[leftmargin=1.4em]
\item Un passage est considéré ponctuel lorsque son retard de départ est inférieur ou égal à cinq minutes.
\item Les événements \texttt{{SKIPPED}} sont comptés séparément : ils ne sont pas assimilés à un retard, mais sont intégrés au score de fiabilité.
\item Le score de fiabilité est égal à la ponctualité, diminuée de deux fois le taux d'arrêts sautés, puis bornée entre 0 et 100. Il sert à prioriser les lignes ; ce n'est pas une mesure de causalité.
\item Seules les observations sorties du flux depuis au moins vingt minutes sont retenues. Ce délai limite l'utilisation de retards encore susceptibles d'être modifiés.
\item Le rapport décrit les données GTFS-RT observées ; il ne permet pas à lui seul d'attribuer une cause opérationnelle aux écarts constatés.
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
    return tex_path.with_suffix(".pdf")


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
            else:
                lines = make_line_stats(scheduled, skipped)
                current = kpis(scheduled, skipped)
                previous_scheduled, previous_skipped, _ = query_observations(conn, previous_month(month), scope)
                previous = kpis(previous_scheduled, previous_skipped) if not previous_scheduled.empty else None
                content = build_latex(month, scope, current, comparison(current, previous), lines, scheduled, collected_at)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        tex_path = args.output_dir / f"vigie-tbm-{month}-{safe_slug(scope.recipient)}.tex"
        tex_path.write_text(content, encoding="utf-8")
        print(f"Rapport LaTeX généré : {tex_path}")
        if args.compile:
            print(f"PDF généré : {compile_pdf(tex_path)}")
    except (FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
