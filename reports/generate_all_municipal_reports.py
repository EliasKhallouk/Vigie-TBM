#!/usr/bin/env python3
"""Génère en une commande un rapport territorial pour chaque commune TBM."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "vigie_tbm.db"
REPORT_GENERATOR = PROJECT_ROOT / "reports" / "generate_monthly_report.py"


def slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère tous les rapports mensuels communaux Vigie TBM.")
    parser.add_argument("--month", required=True, help="Mois analysé au format AAAA-MM.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB, help="Base SQLite à analyser.")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / "output", help="Répertoire racine des rapports.")
    parser.add_argument("--compile", action="store_true", help="Compile les rapports en PDF avec pdflatex.")
    parser.add_argument("--communes", help="Sous-ensemble facultatif de communes, séparées par des virgules (utile pour un test).")
    args = parser.parse_args()
    if not args.db_path.exists():
        parser.error(f"Base introuvable : {args.db_path}")

    with sqlite3.connect(args.db_path) as conn:
        has_mapping = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stop_municipalities')"
        ).fetchone()[0]
        if not has_mapping:
            parser.error("Rattachement communal absent. Lancez d'abord src/scripts/assign_stop_municipalities.py.")
        municipalities = [row[0] for row in conn.execute(
            "SELECT commune_name FROM stop_municipalities GROUP BY commune_name ORDER BY commune_name"
        )]
    if args.communes:
        requested = {name.strip().casefold() for name in args.communes.split(",") if name.strip()}
        municipalities = [name for name in municipalities if name.casefold() in requested]
        unknown = requested - {name.casefold() for name in municipalities}
        if unknown:
            parser.error(f"Commune(s) inconnue(s) : {', '.join(sorted(unknown))}")

    batch_dir = args.output_dir / args.month / "communes"
    batch_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    for index, municipality in enumerate(municipalities, start=1):
        destination = batch_dir / slug(municipality)
        command = [
            sys.executable, str(REPORT_GENERATOR), "--month", args.month,
            "--db-path", str(args.db_path), "--recipient", f"Mairie de {municipality}",
            "--communes", municipality, "--output-dir", str(destination),
        ]
        if args.compile:
            command.append("--compile")
        print(f"[{index}/{len(municipalities)}] {municipality}")
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            failures.append((municipality, result.stderr.strip() or result.stdout.strip()))
            print(f"  ÉCHEC : {failures[-1][1]}", file=sys.stderr)
            continue

    # Cleanup: remove stale index.csv if it exists
    index_csv = batch_dir / "index.csv"
    if index_csv.exists():
        index_csv.unlink()
    print(f"{len(municipalities)} rapports générés. Répertoire : {batch_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
