"""Proactivity, earned: say something only when the record already answers it.

This is the one capability the direction deliberately deferred. A bot that
interrupts you wrongly gets muted, and there is no recovery from that — so it
was held back until Cairn actually understood conversations. It now does, and
this is the narrowest useful version: when someone re-opens a question the team
already settled, point at the decision and its source. Nothing else.

Three rules keep it from becoming noise:

* **Only when the record answers it.** A question, and a strong semantic match
  against a live decision. Not "related", not "interesting" — answers it.
* **Once.** Per decision, per channel, per cooldown. Re-raising the same reminder
  is precisely how a helpful bot becomes a muted one.
* **Off unless asked for.** ``CAIRN_RESURFACE=on``.

It also never says "you already decided this, stop talking" — the wording offers
the record and gets out of the way, because the team may be deliberately
revisiting it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger("cairn.resurface")

# Looks like re-opening a settled question rather than making a new statement.
_QUESTION_RE = re.compile(
    r"(\?\s*$)|"
    r"\b(should we|do we|are we|why (?:do|are|did)|what(?:'s| is| are) (?:our|the) "
    r"|which (?:one|option|approach|database|tool)|how do we|can we|"
    r"remind me|does anyone (?:know|remember)|what did we (?:decide|agree)|"
    r"wondering (?:if|whether)|thoughts on|any reason (?:we|not)|"
    r"i think we should|maybe we should|we could)\b",
    re.IGNORECASE,
)

# Distance below which a memory genuinely answers the question. Deliberately
# tighter than recall's: recall may return something loosely relevant and let the
# model judge, but here there is no judge — an unprompted message is the output.
_MAX_DISTANCE = 0.30
_COOLDOWN_S = 24 * 3600

_seen: dict[str, float] = {}


def enabled() -> bool:
    return os.environ.get("CAIRN_RESURFACE", "off").strip().lower() in {"on", "1", "true", "yes"}


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def looks_like_a_question(text: str) -> bool:
    """Is this re-opening something, rather than stating something?

    Statements are excluded on purpose: a decision being *made* is capture's job,
    and answering it with "you already decided this" would be both wrong and
    annoying.
    """
    return bool(text and text.strip() and _QUESTION_RE.search(text))


def _cooling_down(key: str) -> bool:
    now = time.time()
    cooldown = _float_env("CAIRN_RESURFACE_COOLDOWN_H", 24) * 3600 or _COOLDOWN_S
    for k, ts in list(_seen.items()):
        if now - ts > cooldown:
            _seen.pop(k, None)
    if now - _seen.get(key, 0.0) < cooldown:
        return True
    _seen[key] = now
    return False


def _match(store: Any, text: str, scope: tuple[str, str] | None) -> tuple | None:
    """The single live decision that answers `text`, or None.

    Returns ``(id, content, author, channel, created_at, distance)``.
    """
    try:
        vec = store._vector_literal(text) if hasattr(store, "_vector_literal") else None
    except Exception:  # noqa: BLE001
        vec = None
    if vec is None:
        from cairn.vector_store import _vector_literal  # noqa: PLC0415
        vec = _vector_literal(store.embedder.embed_one(text))

    where, params = ["superseded_by IS NULL"], []
    if scope:
        where.append(f"{scope[0]} = ?")
        params.append(scope[1])
    try:
        row = store._conn.execute(
            f"""SELECT id, content, author, channel, created_at,
                       vector_distance_cos(embedding, vector32(?))
                FROM memory_entries WHERE {' AND '.join(where)}
                ORDER BY 6 ASC LIMIT 1""",
            (vec, *params),
        ).fetchone()
    except Exception as err:  # noqa: BLE001
        log.info("resurface lookup failed: %s", err)
        return None
    if not row or row[5] is None:
        return None
    if float(row[5]) > _float_env("CAIRN_RESURFACE_DISTANCE", _MAX_DISTANCE):
        return None
    return row


def format_note(content: str, author: Any, channel: Any, created_at: Any) -> str:
    """Offer the record; do not police the conversation."""
    from cairn.decisions import _cite  # noqa: PLC0415

    cite = _cite(author, channel, created_at)
    tail = f"\n_— {cite}_" if cite else ""
    return f":round_pushpin: This came up before — {content}{tail}"


def check(text: str, *, channel: str | None = None, team: str | None = None,
          source: str | None = None) -> str | None:
    """A note worth posting for this message, or None.

    Never raises: this runs alongside ordinary message handling and a failure
    here must not disturb it.
    """
    if not (enabled() and looks_like_a_question(text)):
        return None
    try:
        from cairn import context as reqctx  # noqa: PLC0415
        from cairn.agent import _memory_store  # noqa: PLC0415

        store = _memory_store()
        if not hasattr(store, "_conn"):
            return None                      # needs the hybrid (vector) backend
        match = _match(store, text, reqctx.scope_for(channel, team, source=source))
        if not match:
            return None
        mem_id, content, author, mem_channel, created_at, distance = match
        if _cooling_down(f"{channel}:{mem_id}"):
            log.debug("resurface skipped, already raised: %s", mem_id)
            return None
        log.info("resurfacing decision id=%s (distance=%.3f) in channel=%s",
                 mem_id, distance, channel)
        return format_note(content, author, mem_channel, created_at)
    except Exception:  # noqa: BLE001
        log.exception("resurface check failed silently")
        return None
