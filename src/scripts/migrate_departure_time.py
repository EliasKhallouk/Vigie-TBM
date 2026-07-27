import argparse
import sqlite3
from pathlib import Path


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ajoute la colonne departure_time à observations (SQLite).")
    parser.add_argument("--db-path", default="data/vigie_tbm.db", help="Chemin vers la base SQLite.")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Base introuvable: {db_path}")

    sql_path = Path(__file__).resolve().parents[1] / "sql" / "001_add_departure_time.sql"
    migration_sql = sql_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(db_path)
    try:
        if column_exists(conn, "observations", "departure_time"):
            print("Migration ignorée: la colonne observations.departure_time existe déjà.")
            return

        conn.execute(migration_sql)
        conn.commit()
        print("Migration appliquée: colonne observations.departure_time ajoutée.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
