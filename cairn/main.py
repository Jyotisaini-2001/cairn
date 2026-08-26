"""Cairn entrypoint. Loads .env, configures logging, starts the Slack app."""
from __future__ import annotations

import atexit
import logging
import os

from dotenv import load_dotenv


def _flush_dbs() -> None:
    """Checkpoint all SQLite WAL files on clean exit so the next start finds a
    consistent DB regardless of how the process stops (Ctrl+C, SIGTERM, etc.)."""
    import sqlite3
    from cairn.storage import DEFAULT_DB_PATH

    path = os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)
    for db_path in (path, path.replace(".db", "_vec.db")):
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception:
            pass


def main() -> None:
    load_dotenv()

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    atexit.register(_flush_dbs)

    from cairn import slack_app

    slack_app.start()


if __name__ == "__main__":
    main()