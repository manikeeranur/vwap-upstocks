"""Offline migration and online integrity/backup operations."""
import argparse
import json
from pathlib import Path

from .config import AppSettings
from .database import Database, ProcessLock
from .repository import Repository


def main():
    parser = argparse.ArgumentParser(description="Upstocks SQLite maintenance")
    parser.add_argument("command", choices=("migrate", "check", "backup"))
    parser.add_argument("destination", nargs="?", type=Path)
    args = parser.parse_args()
    settings = AppSettings.from_env()
    database = Database(settings.database_path, settings.sqlite_timeout)
    if args.command == "migrate":
        lock = ProcessLock(database.path)
        lock.acquire()
        try:
            database.initialize()
            Repository(database).migrate_legacy(settings.legacy_dir)
            print(json.dumps(database.check()))
        finally:
            lock.release()
    else:
        if not database.path.is_file():
            parser.error("Database does not exist. Start the app or run migrate first.")
        if args.command == "backup":
            if args.destination is None:
                parser.error("backup requires a destination filename")
            print(database.backup(args.destination))
        else:
            result = database.check()
            print(json.dumps(result))
            if result["integrity"] != "ok" or result["foreign_key_errors"]:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
