#!/usr/bin/env python3
"""Génère les rapports pour toutes les communes + Bordeaux Métropole."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT.parent / "data" / "vigie_tbm.db"
REPORT_GENERATOR = PROJECT_ROOT / "generate_monthly_report.py"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Génère les rapports pour toutes les communes + Bordeaux Métropole."
    )
    parser.add_argument("--month", required=True, help="Mois au format AAAA-MM.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB,
                        help="Base SQLite à analyser.")
    parser.add_argument("--compile", action="store_true",
                        help="Compile aussi les rapports en PDF.")
    parser.add_argument("--communes", nargs="+",
                        help="Sous-ensemble facultatif de communes (utile pour tester).")
    args = parser.parse_args()

    if not args.db_path.exists():
        parser.error(f"Base introuvable : {args.db_path}")

    # Récupérer la liste des communes
    with sqlite3.connect(args.db_path) as conn:
        has_mapping = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='stop_municipalities')"
        ).fetchone()[0]
        if not has_mapping:
            parser.error(
                "Rattachement communal absent. "
                "Lancez d'abord src/scripts/assign_stop_municipalities.py."
            )
        communes = [
            row[0] for row in conn.execute(
                "SELECT commune_name FROM stop_municipalities "
                "GROUP BY commune_name ORDER BY commune_name"
            )
        ]

    if args.communes:
        requested = {name.strip().casefold() for name in args.communes}
        communes = [n for n in communes if n.casefold() in requested]
        unknown = requested - {n.casefold() for n in communes}
        if unknown:
            parser.error(f"Commune(s) inconnue(s) : {', '.join(sorted(unknown))}")

    total = len(communes) + 1  # +1 pour Bordeaux Métropole

    # 1. Rapport Bordeaux Métropole
    print(f"[1/{total}] Bordeaux Métropole et TBM")
    cmd = [
        sys.executable, str(REPORT_GENERATOR),
        "--month", args.month,
        "--recipient", "Bordeaux Métropole et TBM",
    ]
    if args.compile:
        cmd.append("--compile")
    result = subprocess.run(cmd)
    if result.returncode:
        print(f"  ÉCHEC Bordeaux Métropole", file=sys.stderr)

    # 2. Rapports communaux
    failures = []
    for idx, commune in enumerate(communes, start=2):
        print(f"[{idx}/{total}] {commune}")
        cmd = [
            sys.executable, str(REPORT_GENERATOR),
            "--month", args.month,
            "--recipient", f"Mairie de {commune}",
            "--communes", commune,
        ]
        if args.compile:
            cmd.append("--compile")
        result = subprocess.run(cmd, text=True, capture_output=True)
        if result.returncode:
            failures.append((commune, result.stderr.strip() or result.stdout.strip()))
            print(f"  ÉCHEC : {failures[-1][1]}", file=sys.stderr)

    print(f"\n{total} rapports générés.")
    if failures:
        print(f"{len(failures)} échec(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
