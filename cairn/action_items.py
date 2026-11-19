"""Action-item extraction and tracking for Cairn.

When someone says "John will fix X by Friday" in Slack, Cairn detects it,
extracts owner/task/deadline, stores it, and nudges the owner as the deadline
approaches via ambient mode.

Table: action_items
  - owner_slack_id: Slack user ID if resolved from @mention, else NULL
  - owner_name: display name or raw text
  - task: what needs to be done
  - deadline_ts: Unix timestamp of the deadline (NULL if no deadline mentioned)
  - status: open | done | cancelled
  - nudged_at: last time ambient posted a nudge (Unix ts)
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from cairn.llm import extract_json, first_human_mention
from cairn.storage import DEFAULT_DB_PATH

log = logging.getLogger("cairn.actions")

_lock = threading.RLock()  # reentrant: save_action_item() calls _already_saved() under the same lock
_conn: sqlite3.Connection | None = None

_ACTION_RE = re.compile(
    r"\b("
    r"will\s+(fix|do|handle|implement|check|review|update|send|finish|complete|work\s+on|look\s+into|take\s+care\s+of)|"
    r"(is\s+going\s+to|is\s+gonna|gonna|going\s+to)\s+(fix|do|handle|implement|check|review|update|send|finish|complete)|"
    r"(assigned\s+to|owned\s+by|on\s+point\s+for)|"
    r"(to[\s-]do|action\s+item|follow[\s-]up):"
    r")\b",
    re.IGNORECASE,
)


def looks_like_action_item(text: str) -> bool:
    return bool(_ACTION_RE.search(text))


def _db() -> sqlite3.Connection:
    """One persistent connection shared across threads, guarded by _lock at every
    call site. Opens the file once; never runs integrity_check or renames the DB
    (that churned the inode and broke external viewers). Same file as the
    telemetry store — SQLite handles the two connections via its own file locks."""
    global _conn
    if _conn is None:
        path = os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)
        Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode = DELETE")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS action_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts      INTEGER NOT NULL DEFAULT (unixepoch()),
                app             TEXT,
                channel         TEXT,
                thread_ts       TEXT,
                team            TEXT,
                owner_slack_id  TEXT,
                owner_name      TEXT,
                task            TEXT NOT NULL,
                deadline_ts     INTEGER,
                deadline_raw    TEXT,
                raw_text        TEXT,
                status          TEXT NOT NULL DEFAULT 'open',
                nudged_at       INTEGER
            )
        """)
        _conn.commit()
    return _conn


def extract_action_item(text: str, raw_text: str) -> dict | None:
    """Use a focused LLM call to extract action-item details. Returns None if
    the message doesn't contain a clear action item."""
    slack_mention = first_human_mention(raw_text)
    today = time.strftime("%Y-%m-%d")
    prompt = (
        f"Today is {today}. Extract the action item from this Slack message and return ONLY valid JSON.\n"
        f"Message: {text}\n\n"
        f'Return exactly: {{"owner":"person responsible (name or @mention)","task":"what they need to do",'
        f'"deadline":"YYYY-MM-DD if a date/day is mentioned, else null","is_action_item":true/false}}\n'
        f"Set is_action_item=false if this is just conversation with no clear commitment or task."
    )
    data = extract_json(prompt, max_tokens=200)
    if not data or not data.get("is_action_item"):
        return None
    deadline_ts = None
    deadline_raw = data.get("deadline")
    if deadline_raw:
        try:
            import datetime
            dt = datetime.datetime.strptime(deadline_raw, "%Y-%m-%d")
            deadline_ts = int(dt.replace(hour=23, minute=59).timestamp())  # end of that day
        except ValueError:
            pass
    return {
        "owner_name": data.get("owner", ""),
        "owner_slack_id": _extract_user_id(slack_mention) if slack_mention else None,
        "task": data.get("task", text[:200]),
        "deadline_ts": deadline_ts,
        "deadline_raw": deadline_raw,
        "raw_text": text,
    }


def _extract_user_id(mention: str) -> str | None:
    m = re.search(r"<@([A-Z0-9]+)>", mention)
    return m.group(1) if m else None


def _already_saved(channel: str, thread_ts: str, task: str) -> bool:
    """True if an identical action item was saved in the last 30 seconds (dedup across apps)."""
    with _lock:
        row = _db().execute(
            "SELECT 1 FROM action_items WHERE channel=? AND thread_ts=? AND task=? AND created_ts > ?",
            (channel, thread_ts, task, int(time.time()) - 30),
        ).fetchone()
    return row is not None


def save_action_item(
    item: dict,
    app: str,
    channel: str,
    thread_ts: str,
    team: str | None,
) -> int:
    """Persist an extracted action item. Returns the new row id."""
    with _lock:
        if _already_saved(channel, thread_ts, item["task"]):
            log.info("cairn.actions: skipping duplicate action item task=%r", item["task"][:60])
            return -1
        conn = _db()
        cur = conn.execute(
            """INSERT INTO action_items
               (app, channel, thread_ts, team, owner_slack_id, owner_name,
                task, deadline_ts, deadline_raw, raw_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                app, channel, thread_ts, team,
                item.get("owner_slack_id"), item.get("owner_name"),
                item["task"], item.get("deadline_ts"), item.get("deadline_raw"),
                item.get("raw_text"),
            ),
        )
        conn.commit()
        log.info("cairn.actions: saved action item id=%d task=%r owner=%r deadline=%s",
                 cur.lastrowid, item["task"][:60], item.get("owner_name"), item.get("deadline_raw"))
        return cur.lastrowid


def get_due_items(nudge_hours: int = 24) -> list[dict]:
    """Return open action items whose deadline is within `nudge_hours` hours
    and haven't been nudged in the last `nudge_hours` hours."""
    now = int(time.time())
    horizon = now + nudge_hours * 3600
    nudge_cutoff = now - nudge_hours * 3600
    with _lock:
        rows = _db().execute(
            """SELECT id, app, channel, thread_ts, team, owner_slack_id, owner_name,
                      task, deadline_ts, deadline_raw
               FROM action_items
               WHERE status = 'open'
                 AND deadline_ts IS NOT NULL
                 AND deadline_ts <= ?
                 AND (nudged_at IS NULL OR nudged_at < ?)
               ORDER BY deadline_ts ASC""",
            (horizon, nudge_cutoff),
        ).fetchall()
    cols = ["id", "app", "channel", "thread_ts", "team", "owner_slack_id",
            "owner_name", "task", "deadline_ts", "deadline_raw"]
    return [dict(zip(cols, r)) for r in rows]


def get_overdue_items() -> list[dict]:
    """Return open items whose deadline has passed."""
    now = int(time.time())
    with _lock:
        rows = _db().execute(
            """SELECT id, app, channel, thread_ts, team, owner_slack_id, owner_name,
                      task, deadline_ts, deadline_raw
               FROM action_items
               WHERE status = 'open' AND deadline_ts IS NOT NULL AND deadline_ts < ?
               ORDER BY deadline_ts ASC""",
            (now,),
        ).fetchall()
    cols = ["id", "app", "channel", "thread_ts", "team", "owner_slack_id",
            "owner_name", "task", "deadline_ts", "deadline_raw"]
    return [dict(zip(cols, r)) for r in rows]


def mark_nudged(item_id: int) -> None:
    with _lock:
        conn = _db()
        conn.execute("UPDATE action_items SET nudged_at = ? WHERE id = ?", (int(time.time()), item_id))
        conn.commit()


def mark_done(item_id: int) -> None:
    with _lock:
        conn = _db()
        conn.execute("UPDATE action_items SET status = 'done' WHERE id = ?", (item_id,))
        conn.commit()


def list_open_items(channel: str | None = None) -> list[dict]:
    """List open action items, optionally filtered by channel."""
    with _lock:
        if channel:
            rows = _db().execute(
                "SELECT id, owner_name, task, deadline_raw, channel, thread_ts FROM action_items "
                "WHERE status = 'open' AND channel = ? ORDER BY deadline_ts ASC NULLS LAST",
                (channel,),
            ).fetchall()
        else:
            rows = _db().execute(
                "SELECT id, owner_name, task, deadline_raw, channel, thread_ts FROM action_items "
                "WHERE status = 'open' ORDER BY deadline_ts ASC NULLS LAST",
            ).fetchall()
    cols = ["id", "owner_name", "task", "deadline_raw", "channel", "thread_ts"]
    return [dict(zip(cols, r)) for r in rows]
