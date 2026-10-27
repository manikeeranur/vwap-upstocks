"""Offline migration, online integrity/backup operations, and managing the people who use the app."""
import argparse
import getpass
import json
from pathlib import Path

from .accounts import hash_password

from .config import AppSettings
from .database import Database, ProcessLock
from .repository import Repository


def main():
    parser = argparse.ArgumentParser(description="Upstocks SQLite maintenance")
    parser.add_argument("command", choices=("migrate", "check", "backup", "add-user", "set-password", "users"))
    parser.add_argument("destination", nargs="?", help="backup file, or the username for add-user / set-password")
    args = parser.parse_args()
    settings = AppSettings.from_env()
    database = Database(settings.database_path, settings.sqlite_timeout)
    if args.command in ("add-user", "set-password", "users"):
        if not database.path.is_file():
            parser.error("Database does not exist. Start the app or run migrate first.")
        database.initialize()
        repo = Repository(database)
        if args.command == "users":
            for u in repo.users():
                print(f"{u['id']:>3}  {u['username']:<20} telegram={'yes' if u['telegram_chat_id'] else 'no'}")
            return
        if not args.destination:
            parser.error(f"{args.command} needs a username")
        password = getpass.getpass(f"Password for {args.destination}: ")
        if len(password) < 8 or password != getpass.getpass("Repeat password: "):
            parser.error("Passwords must match and be at least 8 characters")
        user = repo.user(username=args.destination)
        if args.command == "add-user":
            if user:
                parser.error("That username already exists")
            print("added user", repo.add_user(args.destination, hash_password(password)))
        else:
            if not user:
                parser.error("No such user")
            repo.set_password(user["id"], hash_password(password))
            print("password updated for", args.destination)
        return
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
            print(database.backup(Path(args.destination)))
        else:
            result = database.check()
            print(json.dumps(result))
            if result["integrity"] != "ok" or result["foreign_key_errors"]:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
