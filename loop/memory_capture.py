"""Passive long-term memory capture from channel messages (gaps B / I / K).

Today Loop only writes to long-term memory when the agent explicitly calls
``add_memory`` during an ``@Loop`` turn — so a decision made in a channel Loop
merely *observes* is lost, and "what did we decide about X" later finds nothing.

When ``LOOP_MEMORY_CAPTURE=on`` this module distills observe-only channel messages
that state a **decision** or a **commitment** into a self-contained sentence and
saves it to Loop's long-term (vector) memory with provenance — so recall works
even when nobody mentioned Loop. Off by default.

Kept cheap and quiet with a two-stage filter, mirroring ``action_items.py``:
  1. a regex gate (no model call on chit-chat), then
  2. a focused extraction call that must return ``is_memorable=true``.

Provenance (author / channel / team / source) is stamped so recall stays scoped
the same way agent-saved memories are (``LOOP_MEMORY_SCOPE``), and exact-content
dedup on the store side stops the same line being saved twice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from loop.action_items import looks_like_action_item
from loop.llm import extract_json

log = logging.getLogger("loop.memcapture")

# Decision / agreement phrasing. Commitments ("X will fix Y") are covered by
# reusing action_items.looks_like_action_item, so this only adds the decision side.
_DECISION_RE = re.compile(
    r"\b("
    r"we(?:'?ll| will| are going to| decided| agreed| chose| picked| settled)|"
    r"let'?s (?:go with|use|stick with|do)|going with|decision:|"
    r"the plan is|final call|we'?re using|agreed to|settled on|"
    r"moving forward with|from now on|going forward|for now we"
    r")\b",
    re.IGNORECASE,
)

_store_inst: Any = None


def enabled() -> bool:
    return os.environ.get("LOOP_MEMORY_CAPTURE", "off").strip().lower() in {"on", "1", "true", "yes"}


def looks_memorable(text: str) -> bool:
    """Cheap gate: does this look like a durable decision or a commitment?"""
    return bool(_DECISION_RE.search(text)) or looks_like_action_item(text)


def _store() -> Any:
    """The long-term memory store, lazily built once and cached. Uses the same
    backend selector as the agent so captures land where recall reads from."""
    global _store_inst
    if _store_inst is None:
        from loop.agent import _memory_store  # noqa: PLC0415 (avoid import cycle at module load)

        _store_inst = _memory_store()
    return _store_inst


def _extract_memory(text: str) -> str | None:
    """Focused model call: return a one-line memory worth keeping, or None."""
    prompt = (
        "Decide if this Slack message states a durable team DECISION, agreement, or a "
        "concrete COMMITMENT worth remembering long-term. Return ONLY valid JSON:\n"
        '{"is_memorable":true/false,"memory":"one self-contained sentence capturing '
        'the decision or commitment (who + what), or empty string"}\n'
        "Set is_memorable=false for questions, opinions, chit-chat, or vague talk.\n\n"
        f"Message: {text}"
    )
    data = extract_json(prompt, max_tokens=200)
    if not data or not data.get("is_memorable"):
        return None
    return (data.get("memory") or "").strip() or None


def _save(content: str, metadata: dict[str, Any]) -> None:
    """Persist to the async store from this (sync, threaded) Bolt handler."""
    store = _store()
    try:
        asyncio.run(store.add(content, metadata))
    except RuntimeError:
        # A loop is already running in this thread (rare) — use a fresh one.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(store.add(content, metadata))
        finally:
            loop.close()


def capture(
    text: str,
    *,
    author: str | None = None,
    channel: str | None = None,
    team: str | None = None,
    source: str | None = None,
) -> str | None:
    """Two-stage passive capture. Returns the saved memory string, or None.

    Never raises — a capture failure must not affect the message path. There is
    no active RequestState here (this runs on observe-only messages, outside an
    agent turn), so provenance is passed explicitly and merged by the store."""
    if not enabled() or not text or not looks_memorable(text):
        return None
    try:
        memory = _extract_memory(text)
        if not memory:
            return None
        metadata = {
            "kind": "decision",
            **({"author": author} if author else {}),
            **({"channel": channel} if channel else {}),
            **({"team": team} if team else {}),
            **({"source": source} if source else {}),
        }
        _save(memory, metadata)
        log.info("[%s] memory captured channel=%s: %r", source, channel, memory[:80])
        return memory
    except Exception:  # noqa: BLE001
        log.exception("passive memory capture failed silently")
        return None
