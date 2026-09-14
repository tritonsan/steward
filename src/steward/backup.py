"""Non-destructive SQLite snapshot and restore rehearsal.

Cloud PostgreSQL backups use RDS snapshots/PITR; this command never connects to
or replaces a production database. Destinations must not already exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def inspect_database(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("database integrity verification failed")
        # Canonical sorted logical content, independent of row insertion order/page layout.
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        digest = hashlib.sha256()
        counts = {}
        for table in tables:
            identifier = '"' + table.replace('"', '""') + '"'
            rows = sorted(
                json.dumps(row, default=str)
                for row in connection.execute(f"SELECT * FROM {identifier}")
            )
            counts[table] = len(rows)
            digest.update((table + "\n" + "\n".join(rows)).encode())
        return {"integrity": integrity, "logical_sha256": digest.hexdigest(), "rows": counts}


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError("source database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidental replacement, including concurrent callers.
    with destination.open("xb"):
        pass
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src,
        sqlite3.connect(destination) as target,
    ):
        src.backup(target)
    return inspect_database(destination)


def rehearsal(source, backup_path, restore_path):
    backup = snapshot(source, backup_path)
    restored = snapshot(backup_path, restore_path)
    if backup != restored:
        raise ValueError("restored database differs from backup")
    return {"engine": "sqlite", "restored_to_new_file": True, "passed": True, **restored}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("backup", type=Path)
    parser.add_argument("restore", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = rehearsal(args.source, args.backup, args.restore)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
