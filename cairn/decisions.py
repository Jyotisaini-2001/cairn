"""The decision log — answering questions *about the record*, not from it.

Recall so far has been a side effect of chatting: the agent gets a handful of
memories injected before it replies, and that is right for "why did we pick
Postgres?". It is the wrong shape for questions about the record itself —
"what did we decide last week", "show me everything about the billing service" —
which want a *list*, ordered, with sources, and more than the five entries a
prompt can afford to carry.

So this reads the store directly rather than going through the agent: a
deterministic listing with citations, which is also the honest answer to "how do
I know it isn't making this up".
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("cairn.decisions")

_MAX_LIMIT = 50


def _store() -> Any:
    from cairn.agent import _memory_store  # noqa: PLC0415 (import cycle at load)
    return _memory_store()


# ── time windows ─────────────────────────────────────────────────────────────
# "last week" is the most natural way to ask for a decision log, and it is a
# question semantic search cannot answer: relevance has nothing to do with it.
_PERIODS = {
    "today": 1, "yesterday": 2, "this week": 7, "last week": 14,
    "past week": 7, "last 7 days": 7, "this month": 30, "last month": 60,
    "past month": 30, "this quarter": 90, "last quarter": 180,
    "recently": 14, "lately": 14,
}
_DAYS_RE = re.compile(r"\b(?:last|past)\s+(\d{1,3})\s*(day|week|month)s?\b", re.IGNORECASE)


def parse_since(text: str) -> float | None:
    """Epoch seconds for the start of a period named in `text`, or None.

    Deliberately coarse: "last week" means "the last couple of weeks" rather
    than an exact calendar boundary, because a decision made on the Friday
    before is what the asker usually wants.
    """
    lowered = (text or "").lower()
    if m := _DAYS_RE.search(lowered):
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return time.time() - days * 86400
    for phrase, days in sorted(_PERIODS.items(), key=lambda kv: -len(kv[0])):
        if phrase in lowered:
            return time.time() - days * 86400
    return None


# ── querying ─────────────────────────────────────────────────────────────────
def _rows(store: Any, *, since: float | None, limit: int,
          scope: tuple[str, str] | None) -> list[tuple]:
    where = ["superseded_by IS NULL"]
    params: list[Any] = []
    if since:
        where.append("created_at >= ?")
        params.append(int(since))
    if scope:
        where.append(f"{scope[0]} = ?")
        params.append(scope[1])
    sql = (f"SELECT content, author, channel, created_at, thread_ts FROM memory_entries "
           f"WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT ?")
    try:
        return store._conn.execute(sql, (*params, limit)).fetchall()
    except Exception as err:  # noqa: BLE001
        log.warning("decision log query failed: %s", err)
        return []


def _cite(author: Any, channel: Any, created_at: Any) -> str:
    bits = []
    if author:
        bits.append(f"<@{author}>")
    if channel:
        bits.append(f"in <#{channel}>")
    if created_at:
        try:
            bits.append(f"on {datetime.fromtimestamp(int(created_at), timezone.utc):%d %b %Y}")
        except Exception:  # noqa: BLE001
            pass
    return " ".join(bits)


def log_decisions(query: str = "", *, limit: int = 10,
                  scope: tuple[str, str] | None = None) -> str:
    """A Slack-formatted list of decisions, newest first, each with its source.

    `query` may name a period ("what did we decide last week"); anything else in
    it is ignored here on purpose — this is the *log*, and mixing relevance
    ranking into a chronological listing produces something that is neither.
    """
    limit = max(1, min(int(limit or 10), _MAX_LIMIT))
    since = parse_since(query)
    rows = _rows(_store(), since=since, limit=limit, scope=scope)
    if not rows:
        window = " in that period" if since else ""
        return f"I have no decisions recorded{window} yet."

    when = ""
    if since:
        days = max(1, round((time.time() - since) / 86400))
        when = f" from the last {days} days"
    lines = [f"*{len(rows)} decision(s){when}*, newest first:", ""]
    for content, author, channel, created_at, _thread in rows:
        cite = _cite(author, channel, created_at)
        lines.append(f"• {content}" + (f"\n   _— {cite}_" if cite else ""))
    return "\n".join(lines)


# ── agent-facing tool ────────────────────────────────────────────────────────
from strands import tool  # noqa: E402


@tool
def decision_log(period: str = "", limit: int = 10) -> str:
    """List the team's recorded decisions, newest first, each with its source.

    Use this for questions ABOUT THE RECORD rather than questions answered FROM
    it: "what did we decide last week", "show me recent decisions", "what have we
    agreed lately". For "why did we pick X?" just answer from the memories
    already injected into your context — this tool returns a list, not an answer.

    Args:
        period: an optional time window in plain words — "last week", "this
            month", "past 3 days". Leave empty for the most recent decisions.
        limit: how many to list (1-50, default 10).
    """
    from cairn import context as reqctx  # noqa: PLC0415

    return log_decisions(period, limit=limit, scope=reqctx.memory_scope())
