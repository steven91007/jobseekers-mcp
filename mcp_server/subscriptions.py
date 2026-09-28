"""Read-only view of the Discord bot's subscriptions and run history.

The bot process owns the database and writes to it on its own schedule, so the
MCP server opens it read-only (``mode=ro``) and never creates or migrates it.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from bot import db as bot_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class BotDbMissing(RuntimeError):
    pass


def db_path() -> Path:
    p = Path(os.getenv("JOBBOT_DB", "").strip() or "data/jobs.db")
    return p if p.is_absolute() else PROJECT_ROOT / p


def _connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        raise BotDbMissing(f"no bot database at {path}; start the Discord bot (python -m bot) first")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def list_subscriptions(active_only: bool = False) -> dict:
    conn = _connect()
    try:
        subs = bot_db.list_subscriptions(conn, active_only=active_only)
    finally:
        conn.close()
    return {
        "count": len(subs),
        "subscriptions": [{**asdict(s), "description": s.describe()} for s in subs],
    }


def bot_status() -> dict:
    conn = _connect()
    try:
        last = bot_db.last_finished_run(conn)
        totals = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(active), 0) AS active, "
            "COALESCE(SUM(consecutive_failures > 0), 0) AS failing FROM subscriptions"
        ).fetchone()
        seen = conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
    finally:
        conn.close()
    return {
        "last_run": dict(last) if last else None,
        "subscriptions": {"total": totals["total"], "active": totals["active"], "failing": totals["failing"]},
        "jobs_already_pushed": seen,
    }
