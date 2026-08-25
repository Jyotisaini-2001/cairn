"""Reading Slack conversations as turns.

One reader, three consumers — this is the substrate Phase 1 sits on:

  * **context** — the recent turns of the thread (or channel) the agent is
    answering in, so follow-ups resolve;
  * **capture** — a whole thread to distil into a memory, keeping the *why* and
    not just the concluding line;
  * **backfill** — the same, run over history that predates Cairn.

All three want the same thing: fetch messages, drop the noise, label who spoke,
and stay bounded. Doing it once keeps those three honest with each other — a
thread the agent *reads* is the same thread capture *sees*.

A "turn" is a plain dict (``{"speaker", "text", "ts", "user"}``) rather than a
dataclass because it travels in the request-context envelope and into telemetry.

Nothing here raises: Slack scopes are granted per-workspace and a missing one
must degrade to less context, never a failed reply.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("loop.conversation")

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

# A thread the agent reads is bounded by turns *and* characters: turns keep the
# prompt focused, the char cap stops one pasted stack trace blowing the budget.
DEFAULT_CHAR_CAP = 4000


def strip_mentions(text: str) -> str:
    """Drop `<@U…>` mentions — they are noise to the model and to a saved memory."""
    return _MENTION_RE.sub("", text).strip()


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _flag(name: str, default: str = "on") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"off", "0", "false", "no"}


def to_turns(messages: list[dict], *, exclude_ts: str = "", bot_user_id: str = "",
             limit: int = 10, char_cap: int = DEFAULT_CHAR_CAP) -> list[dict]:
    """Normalise raw Slack messages into bounded, labelled turns (oldest first).

    Drops the message being answered, joins/leave notices and anything that is
    only a mention. ``speaker`` is ``"you"`` for Cairn's own messages so the model
    can tell its own voice apart from the humans'.
    """
    turns: list[dict] = []
    for m in messages or []:
        if exclude_ts and m.get("ts") == exclude_ts:
            continue
        if m.get("subtype") in {"channel_join", "channel_leave", "channel_topic",
                                "channel_purpose", "channel_name"}:
            continue
        text = strip_mentions(m.get("text", "") or "")
        if not text:
            continue
        is_bot = bool(m.get("bot_id")) or (bot_user_id and m.get("user") == bot_user_id)
        turns.append({
            "speaker": "you" if is_bot else f"<@{m.get('user')}>",
            "text": text,
            "ts": m.get("ts", ""),
            "user": m.get("user", ""),
        })

    if limit:
        turns = turns[-limit:]
    # Trim oldest-first so the most recent context always survives the cap.
    while turns and sum(len(t["text"]) for t in turns) > char_cap:
        turns.pop(0)
    return turns


def read_thread(client, channel: str, thread_ts: str, *, exclude_ts: str = "",
                bot_user_id: str = "", limit: int = 10,
                char_cap: int = DEFAULT_CHAR_CAP) -> list[dict]:
    """Turns of one thread, oldest first.

    ``thread_ts`` must be the thread's **parent** ts. Passing a reply's own ts
    returns just that message — the silent-empty-context bug from Phase 0.
    """
    if not (thread_ts and limit):
        return []
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts,
                                            limit=limit + 1)
    except Exception as err:  # noqa: BLE001
        log.info("thread unavailable (channel=%s ts=%s): %s", channel, thread_ts, err)
        return []
    return to_turns(resp.get("messages", []), exclude_ts=exclude_ts,
                    bot_user_id=bot_user_id, limit=limit, char_cap=char_cap)


def read_channel(client, channel: str, *, exclude_ts: str = "",
                 bot_user_id: str = "", limit: int = 8,
                 char_cap: int = DEFAULT_CHAR_CAP) -> list[dict]:
    """Recent turns of a channel, oldest first.

    Needed because people do not reliably reply *in* threads: they @-mention in
    the channel, Cairn answers in a thread under that message, and the next
    question is another top-level message. Without this, that whole flow gets no
    context — and the model burns tokens calling the Slack API to find it itself.

    Requires ``channels:history`` (public) / ``groups:history`` (private).
    """
    if not limit:
        return []
    try:
        resp = client.conversations_history(channel=channel, limit=limit + 1)
    except Exception as err:  # noqa: BLE001
        log.info("channel history unavailable (channel=%s): %s", channel, err)
        return []
    # conversations_history returns newest-first; turns are oldest-first.
    messages = list(reversed(resp.get("messages", []) or []))
    return to_turns(messages, exclude_ts=exclude_ts, bot_user_id=bot_user_id,
                    limit=limit, char_cap=char_cap)


def read_context(client, channel: str, *, thread_ts: str = "", in_thread: bool = False,
                 current_ts: str = "", bot_user_id: str = "") -> list[dict]:
    """The conversation to hand the agent for this request.

    In a thread → that thread. A top-level mention → recent channel messages, if
    ``CAIRN_CHANNEL_CONTEXT`` allows. Each is separately switchable because they
    have different privacy shapes: a thread is the conversation you were
    addressed in, a channel scan is everything anyone said nearby.
    """
    if in_thread and _flag("CAIRN_THREAD_CONTEXT"):
        turns = read_thread(client, channel, thread_ts, exclude_ts=current_ts,
                            bot_user_id=bot_user_id,
                            limit=_int_env("CAIRN_THREAD_CONTEXT_LIMIT", 10))
        source = "thread"
    elif not in_thread and _flag("CAIRN_CHANNEL_CONTEXT"):
        turns = read_channel(client, channel, exclude_ts=current_ts,
                             bot_user_id=bot_user_id,
                             limit=_int_env("CAIRN_CHANNEL_CONTEXT_LIMIT", 8))
        source = "channel"
    else:
        turns, source = [], "off"
    log.info("context: %d turn(s) from %s (channel=%s)", len(turns), source, channel)
    return turns
