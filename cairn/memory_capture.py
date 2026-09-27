"""Passive long-term memory capture from channel messages (gaps B / I / K).

Today Cairn only writes to long-term memory when the agent explicitly calls
``add_memory`` during an ``@Cairn`` turn — so a decision made in a channel Cairn
merely *observes* is lost, and "what did we decide about X" later finds nothing.

When ``CAIRN_MEMORY_CAPTURE=on`` this module distills observe-only channel messages
that state a **decision** or a **commitment** into a self-contained sentence and
saves it to Cairn's long-term (vector) memory with provenance — so recall works
even when nobody mentioned Cairn. Off by default.

Kept cheap and quiet with a two-stage filter, mirroring ``action_items.py``:
  1. a regex gate (no model call on chit-chat), then
  2. a focused extraction call that must return ``is_memorable=true``.

Provenance (author / channel / team / source) is stamped so recall stays scoped
the same way agent-saved memories are (``CAIRN_MEMORY_SCOPE``), and exact-content
dedup on the store side stops the same line being saved twice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from cairn.action_items import looks_like_action_item
from cairn.llm import extract_json

log = logging.getLogger("cairn.memcapture")

# Decision / agreement phrasing. Commitments ("X will fix Y") are covered by
# reusing action_items.looks_like_action_item, so this only adds the decision side.
# Cheap prefilter before the model call. It only needs to be *permissive* — the
# extraction step returns is_memorable=false on anything that is not a real
# decision, and the cooldown stops an active thread being re-distilled. Being too
# narrow is the expensive failure: a missed decision is lost silently and for
# good, while a false positive costs one model call.
#
# The first cut matched `let's (go with|use|stick with|do)` — four verbs — and so
# missed "ok let's merge them into one and drop the per-service split", which is
# about as clear a decision as Slack produces.
_DECISION_RE = re.compile(
    r"\b("
    # explicit statements of a decision
    r"we(?:'?ll| will| are going to| decided| agreed| chose| picked| settled)|"
    r"we(?:'?re| are) (?:going with|using|switching|moving|dropping|keeping|adopting|"
    r"replacing|deprecating|standardi[sz]ing)|"
    r"going with|decision:|the plan is|final call|agreed to|settled on|"
    r"moving forward with|from now on|going forward|for now we|"
    # "let's <anything>" — an imperative the room is agreeing to
    r"let'?s \w+|"
    # decision verbs applied to a thing
    r"(?:switch|move|drop|merge|split|consolidate|migrate|adopt|replace|"
    r"deprecate|revert|roll back|standardi[sz]e) (?:it|them|this|that|to|from|"
    r"onto|into|off|back|over)|"
    # scope / policy calls
    r"(?:cap|limit|freeze|block|require|enforce|mandate) (?:it|them|this|the|a|"
    r"all|any)"
    r")\b",
    re.IGNORECASE,
)

# Short assent. Weak on its own, but capture reads the WHOLE conversation, so
# "agreed" after a proposal is exactly the moment a decision becomes one. Bare
# "ok"/"yes" are deliberately excluded: too common in chit-chat to be worth a
# model call.
_AGREEMENT_RE = re.compile(
    r"^\s*(?:"
    r"\+1|lgtm|sgtm|agreed|approved|deal|ship it|do it|"
    r"sounds good|works for me|fine by me|makes sense|let'?s do it|"
    r"ok(?:ay)?[, ]+(?:let'?s|we|agreed|do|go)"
    r")\b",
    re.IGNORECASE,
)

_store_inst: Any = None


def enabled() -> bool:
    return os.environ.get("CAIRN_MEMORY_CAPTURE", "off").strip().lower() in {"on", "1", "true", "yes"}


# Hedged talk is a proposal, not a decision: "we could split it per-service",
# "maybe we should merge them", "what if we drop the split". These trip the
# decision-verb patterns while the room has settled nothing, and Slack is full of
# them. An explicit decision phrase still wins — "we could have, but we decided
# to X" is a decision.
_HEDGE_RE = re.compile(
    r"\b(?:could|might|maybe|perhaps|what if|how about|should we|thinking about|"
    r"wondering|possibly|considering|any thoughts|open to)\b", re.IGNORECASE)

# Phrases that state a settled outcome, and so override a hedge in the same line.
_DECIDED_RE = re.compile(
    r"\b(?:we (?:decided|agreed|chose|picked|settled)|decision:|final call|"
    r"settled on|agreed to|going with|from now on)\b", re.IGNORECASE)


def looks_memorable(text: str) -> bool:
    """Cheap gate: could this message be the moment a decision was made?

    Deliberately permissive — see the note on _DECISION_RE. Three ways in: a
    stated decision, a short assent to one, or a commitment ("X will do Y").
    """
    if not text or not text.strip():
        return False
    if _HEDGE_RE.search(text) and not _DECIDED_RE.search(text):
        return False
    return bool(_DECISION_RE.search(text)
                or _AGREEMENT_RE.search(text)
                or looks_like_action_item(text))


def _store() -> Any:
    """The long-term memory store, lazily built once and cached. Uses the same
    backend selector as the agent so captures land where recall reads from."""
    global _store_inst
    if _store_inst is None:
        from cairn.agent import _memory_store  # noqa: PLC0415 (avoid import cycle at module load)

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


# ── Thread-level capture ─────────────────────────────────────────────────────
# A decision is not a sentence, it is a conversation. Capturing only the line
# that matched the gate keeps the conclusion and throws away the reasoning:
#
#     [12] "Mongo's transactions are flaky for billing"
#     [19] "Postgres means a migration though"
#     [31] "correctness wins, we can eat the migration"
#     [34] "ok let's go with Postgres"        <- only this used to be saved
#
# Six months later "why?" is the question people actually ask, so we distil the
# whole thread: what was decided, why, and what was rejected.
_THREAD_COOLDOWN_S = 15 * 60
_recent_captures: dict[str, float] = {}


def _cooling_down(key: str) -> bool:
    """One capture per conversation per cooldown window.

    An active thread trips the gate on several messages; without this each one
    would re-distil the same discussion. In-process only — a restart forgets,
    and the store's exact-content dedup catches the common repeat.
    """
    now = time.time()
    for k, ts in list(_recent_captures.items()):      # opportunistic cleanup
        if now - ts > _THREAD_COOLDOWN_S:
            _recent_captures.pop(k, None)
    if now - _recent_captures.get(key, 0.0) < _THREAD_COOLDOWN_S:
        return True
    _recent_captures[key] = now
    return False


_FOCUS_MARK = ">>> "


def _render(turns: list[dict], focus_ts: str | None = None) -> str:
    return "\n".join(
        f"{_FOCUS_MARK if focus_ts and t.get('ts') == focus_ts else ''}"
        f"{t.get('speaker', '?')}: {t.get('text', '')}" for t in turns)


# Outside a thread the "conversation" is the last N channel messages, which in a
# live channel is several topics and a few questions. Asked whether *that*
# reached a decision, the model said no — and a decision posted after two
# unrelated questions was dropped, while the same two messages alone distilled
# fine. So the live path names the message being judged; the rest is background.
_FOCUS_RULES = (
    "The message marked >>> just arrived and is the one being judged. Decide "
    "whether IT makes, states or confirms a durable decision. If it is a short "
    "agreement ('+1', 'ship it', 'yeah do that'), the decision is what it agrees "
    "to. The other messages are background: use them only for the WHY and what "
    "was rejected. Unrelated topics or open questions among them do not make the "
    "marked message unresolved. The memory must describe only the marked "
    "message's decision.\n\n"
)


def _extract_from_conversation(turns: list[dict], focus_ts: str | None = None) -> str | None:
    """Focused model call over a whole conversation.

    Asks for the reasoning explicitly — a memory that records *what* without
    *why* cannot answer the question people actually come back with.

    ``focus_ts`` is the message that tripped the gate (the live path). Backfill
    passes none: in a past thread the decision is often not the last message.
    """
    focused = bool(focus_ts) and any(t.get("ts") == focus_ts for t in turns)
    prompt = (
        "Below is a Slack conversation. Decide whether the team reached a durable "
        "DECISION, agreement, or concrete COMMITMENT in it.\n\n"
        "Return ONLY valid JSON:\n"
        '{"is_memorable": true/false, '
        '"memory": "two to four sentences: WHAT was decided, WHY (the reasoning '
        'and any trade-off accepted), and WHAT WAS REJECTED if an alternative was '
        'discussed. Self-contained — it must still make sense to someone reading '
        'it in six months with no other context. Do not add who/where/when; that '
        'is attached automatically."}\n\n'
        "Set is_memorable=false if the conversation is questions, opinions, "
        "brainstorming with no conclusion, chit-chat, or still unresolved. "
        "Prefer false when in doubt: a noisy memory hurts recall.\n\n"
        f"{_FOCUS_RULES if focused else ''}"
        f"Conversation:\n{_render(turns, focus_ts if focused else None)}"
    )
    data = extract_json(prompt, max_tokens=500)
    if not data:
        return None                 # extract_json has already logged the failure
    if not data.get("is_memorable"):
        # Logged so a rejection is distinguishable from a hang or a crash.
        log.info("capture: model judged it not a decision (%d turns)", len(turns))
        return None
    return (data.get("memory") or "").strip() or None


def capture_conversation(
    turns: list[dict],
    *,
    author: str | None = None,
    channel: str | None = None,
    team: str | None = None,
    source: str | None = None,
    thread_ts: str | None = None,
    bypass_cooldown: bool = False,
    require_enabled: bool = True,
    focus_ts: str | None = None,
) -> str | None:
    """Distil a conversation into one memory. Returns it, or None.

    Never raises: capture runs alongside the message path and must never affect
    a reply. `thread_ts` is both stamped as provenance (so a memory can be traced
    back to the discussion it came from) and used as the cooldown key.

    Backfill passes ``bypass_cooldown`` (each historical conversation is distinct,
    and it does its own dedup) and ``require_enabled=False`` (it is an explicit,
    operator-invoked action, not passive listening).
    """
    if (require_enabled and not enabled()) or not turns:
        return None
    key = thread_ts or f"{channel}:{turns[-1].get('ts', '')}"
    if not bypass_cooldown and _cooling_down(key):
        log.debug("capture skipped, cooling down: %s", key)
        return None
    try:
        memory = _extract_from_conversation(turns, focus_ts=focus_ts)
        if not memory:
            return None
        metadata = {
            "kind": "decision",
            **({"author": author} if author else {}),
            **({"channel": channel} if channel else {}),
            **({"team": team} if team else {}),
            **({"source": source} if source else {}),
            **({"thread_ts": thread_ts} if thread_ts else {}),
        }
        _save(memory, metadata)
        log.info("[%s] thread memory captured channel=%s turns=%d: %r",
                 source, channel, len(turns), memory[:100])
        return memory
    except Exception:  # noqa: BLE001
        log.exception("thread memory capture failed silently")
        return None
