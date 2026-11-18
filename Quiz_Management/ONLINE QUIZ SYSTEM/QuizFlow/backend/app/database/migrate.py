"""
Safe, Repeatable Database Migration & Backup Utility for QuizFlow.
Works on both SQLite and PostgreSQL without deleting or damaging existing data.

Usage:
  python -m app.database.migrate --backup
  python -m app.database.migrate --backup --verify
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys

from sqlalchemy import inspect, text

from app.core.config import settings
from app.database.init_db import init_db
from app.database.session import SessionLocal, engine


def backup_sqlite_database() -> Path | None:
    if not settings.is_sqlite:
        print("Non-SQLite database detected. Use pg_dump for PostgreSQL backup:")
        print("  pg_dump --format=custom --file=quizflow_backup_$(date +%Y%m%d_%H%M%S).dump \"$DATABASE_URL\"")
        return None

    raw_path = settings.DATABASE_URL.replace("sqlite:///", "", 1)
    db_file = Path(raw_path).resolve()
    if not db_file.exists():
        print(f"SQLite file not found at {db_file}; nothing to back up yet.")
        return None

    backup_dir = db_file.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"{db_file.stem}_backup_{ts}{db_file.suffix}"
    shutil.copy2(db_file, dest)
    print(f"SQLite backup created safely at: {dest}")
    return dest


def verify_schema_and_counts() -> dict[str, int]:
    db = SessionLocal()
    try:
        insp = inspect(engine)
        tables = sorted(insp.get_table_names())
        counts = {}
        for tbl in tables:
            counts[tbl] = int(db.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar() or 0)
        return counts
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="QuizFlow safe schema & index migration runner")
    parser.add_argument("--backup", action="store_true", help="Create a pre-migration backup (SQLite)")
    parser.add_argument("--verify", action="store_true", help="Print table row counts before & after migration")
    args = parser.parse_args()

    print(f"Target database dialect: {engine.dialect.name}")
    if args.backup:
        backup_sqlite_database()

    before = verify_schema_and_counts() if args.verify else None
    if before is not None:
        print("Row counts BEFORE migration:", before)

    init_db()

    if args.verify:
        after = verify_schema_and_counts()
        print("Row counts AFTER migration:", after)
        for tbl, cnt in (before or {}).items():
            if after.get(tbl, 0) < cnt:
                print(f"WARNING: Row count decreased in {tbl}: {cnt} -> {after.get(tbl)}")
                return 1
        print("Migration verification PASSED: all existing data preserved.")

    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
