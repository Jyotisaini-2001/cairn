"""Backfill: learn from conversations that predate Cairn.

Passive capture only ever sees what happens *after* it is switched on. A team's
memory is the last three years of #eng, so a fresh install knows nothing until
people happen to decide something new — which is the difference between a demo
and a product.

This walks a channel's history, groups it into conversations (a thread, or a run
of consecutive channel messages), and distils the ones that look like decisions
through the same gate and the same extractor the live path uses. Same reader,
same gate, same distillation — a backfilled memory is indistinguishable from one
captured live, including its provenance.

Two things it is careful about:

* **Cost.** Every candidate conversation is one model call. Free tiers are
  measured in tens of requests per day, so `--limit` is small by default and
  `--dry-run` shows what *would* be captured using the (free) gate alone.
* **Repeat runs.** Processed conversations are recorded, so re-running picks up
  where it stopped instead of re-distilling and re-paying for the same threads.

    cairn-backfill --channel C0123 --dry-run
    cairn-backfill --channel C0123 --limit 5
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from typing import Any, Iterator

from loop import conversation, memory_capture

log = logging.getLogger("loop.backfill")

# A run of consecutive channel messages counts as one conversation while the gap
# between them stays under this. Slack has no marker for "this discussion ended",
# so time is the available proxy.
_GAP_S = 30 * 60


# ── state ────────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    path = os.environ.get("DATABASE_PATH", "./data/loop.db")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS backfill_state (
        channel TEXT NOT NULL,
        key     TEXT NOT NULL,
        ts      REAL NOT NULL,
        PRIMARY KEY (channel, key))""")
    conn.commit()
    return conn


def _seen(conn: sqlite3.Connection, channel: str, key: str) -> bool:
    return conn.execute("SELECT 1 FROM backfill_state WHERE channel=? AND key=?",
                        (channel, key)).fetchone() is not None


def _mark(conn: sqlite3.Connection, channel: str, key: str) -> None:
    conn.execute("INSERT OR REPLACE INTO backfill_state VALUES (?,?,?)",
                 (channel, key, time.time()))
    conn.commit()


# ── reading ──────────────────────────────────────────────────────────────────
def _history(client, channel: str, oldest: float | None, max_messages: int) -> list[dict]:
    """Channel messages, oldest first, paged."""
    out: list[dict] = []
    cursor = None
    while len(out) < max_messages:
        kw: dict[str, Any] = {"channel": channel, "limit": 200}
        if oldest:
            kw["oldest"] = str(oldest)
        if cursor:
            kw["cursor"] = cursor
        try:
            resp = client.conversations_history(**kw)
        except Exception as err:  # noqa: BLE001
            log.warning("history failed (channel=%s): %s", channel, err)
            break
        out.extend(resp.get("messages", []) or [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    out.sort(key=lambda m: float(m.get("ts", 0) or 0))
    return out[:max_messages]


def iter_conversations(client, channel: str, *, bot_user_id: str = "",
                       oldest: float | None = None,
                       max_messages: int = 1000) -> Iterator[tuple[str, list[dict]]]:
    """Yield ``(key, turns)`` for each conversation in a channel's history.

    A threaded discussion is one conversation, keyed by its parent ts. Loose
    channel messages are grouped into runs separated by a gap of silence — the
    closest thing Slack gives us to "and then the topic changed".
    """
    messages = _history(client, channel, oldest, max_messages)
    loose: list[dict] = []
    last_ts = 0.0

    for m in messages:
        ts = float(m.get("ts", 0) or 0)
        if m.get("thread_ts") and m.get("thread_ts") != m.get("ts"):
            continue                      # a reply; covered by its parent below
        if m.get("reply_count"):
            if loose:
                yield f"run:{loose[0].get('ts')}", conversation.to_turns(
                    loose, bot_user_id=bot_user_id, limit=0,
                    char_cap=conversation.DEFAULT_CHAR_CAP)
                loose = []
            turns = conversation.read_thread(
                client, channel, m["ts"], bot_user_id=bot_user_id, limit=50)
            if turns:
                yield m["ts"], turns
            last_ts = ts
            continue

        if loose and ts - last_ts > _GAP_S:
            yield f"run:{loose[0].get('ts')}", conversation.to_turns(
                loose, bot_user_id=bot_user_id, limit=0,
                char_cap=conversation.DEFAULT_CHAR_CAP)
            loose = []
        loose.append(m)
        last_ts = ts

    if loose:
        yield f"run:{loose[0].get('ts')}", conversation.to_turns(
            loose, bot_user_id=bot_user_id, limit=0,
            char_cap=conversation.DEFAULT_CHAR_CAP)


# ── running ──────────────────────────────────────────────────────────────────
def looks_worth_distilling(turns: list[dict]) -> bool:
    """Does any *human* turn trip the memorability gate? Free — no model call.

    Cairn's own messages are excluded deliberately. Its replies are full of
    decision language — "We decided to use Postgres for billing because…",
    "Saved to memory: …", "Per @alice in #eng, the team agreed to…" — because it
    is reporting decisions back. Distilling those would feed the bot's own output
    into its memory, which it would then cite: a citation whose source is the
    citation. For a product whose whole claim is provenance, that is the one
    failure that cannot be allowed.
    """
    return any(memory_capture.looks_memorable(t.get("text", ""))
               for t in turns if t.get("speaker") != "you")


def backfill_channel(client, channel: str, *, bot_user_id: str = "",
                     team: str | None = None, source: str = "backfill",
                     oldest: float | None = None, max_messages: int = 1000,
                     limit: int = 10, dry_run: bool = False) -> dict:
    """Distil past conversations in one channel. Returns a stats dict.

    ``limit`` caps *model calls*, not conversations scanned — the scan is free,
    the distillation is not.
    """
    conn = _db()
    stats = {"scanned": 0, "candidates": 0, "captured": 0, "skipped_seen": 0}

    for key, turns in iter_conversations(client, channel, bot_user_id=bot_user_id,
                                         oldest=oldest, max_messages=max_messages):
        stats["scanned"] += 1
        if not turns or not looks_worth_distilling(turns):
            continue
        stats["candidates"] += 1
        if _seen(conn, channel, key):
            stats["skipped_seen"] += 1
            continue
        if dry_run:
            # Show the human line that tripped the gate, not the last message —
            # the last message is usually Cairn's reply, which is misleading.
            trigger = next((t.get("text", "") for t in turns
                            if t.get("speaker") != "you"
                            and memory_capture.looks_memorable(t.get("text", ""))), "")
            log.info("[dry-run] %s (%d turns) ← %r", key, len(turns), trigger[:72])
            continue
        if stats["captured"] >= limit:
            log.info("limit reached (%d captures); re-run to continue", limit)
            break

        # Attribute to the human whose message stated the decision — not the
        # last turn, which is usually Cairn's own reply.
        author = next((t.get("user") for t in turns
                       if t.get("speaker") != "you"
                       and memory_capture.looks_memorable(t.get("text", ""))), None)
        memory = memory_capture.capture_conversation(
            turns, author=author, channel=channel, team=team,
            source=source, thread_ts=key if not key.startswith("run:") else None,
            bypass_cooldown=True, require_enabled=False)
        _mark(conn, channel, key)         # mark either way: a "no" is also a result
        if memory:
            stats["captured"] += 1
            log.info("captured from %s: %r", key, memory[:90])

    return stats


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(message)s")

    ap = argparse.ArgumentParser(description="Backfill Cairn's memory from Slack history.")
    ap.add_argument("--channel", help="channel id, e.g. C0123ABCD")
    ap.add_argument("--recheck", action="store_true",
                    help="with --compact: ignore cached verdicts and ask again "
                         "(the judgement is a model call, so it can differ)")
    ap.add_argument("--compact", action="store_true",
                    help="skip Slack; collapse duplicates and retire superseded "
                         "memories already in the store")
    ap.add_argument("--days", type=int, default=0, help="only look back this many days (0 = all)")
    ap.add_argument("--limit", type=int, default=10, help="max conversations to distil (model calls)")
    ap.add_argument("--max-messages", type=int, default=1000, help="max messages to scan")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be distilled, without any model calls")
    args = ap.parse_args()

    if args.compact:
        from loop.agent import _memory_store
        store = _memory_store()
        if not hasattr(store, "compact"):
            print("compaction needs the hybrid memory backend (CAIRN_MEMORY_BACKEND=hybrid)")
            return 1
        stats = store.compact(dry_run=args.dry_run, limit=args.limit,
                              recheck=args.recheck)
        print(f"\nscanned {stats['scanned']} memories, "
              f"{stats['duplicates']} duplicate(s), {stats['superseded']} superseded, "
              f"{stats['calls']} model call(s)" + (" [dry run]" if args.dry_run else ""))
        return 0

    if not args.channel:
        print("--channel is required (or use --compact)")
        return 1

    from slack_sdk import WebClient
    token = os.environ.get("SLACK_BOT_TOKEN") or next(
        (v for k, v in os.environ.items() if k.startswith("SLACK_BOT_TOKEN_") and v), "")
    if not token:
        print("No SLACK_BOT_TOKEN found in the environment.")
        return 1
    client = WebClient(token=token)
    try:
        bot_user_id = client.auth_test().get("user_id", "")
    except Exception:  # noqa: BLE001
        bot_user_id = ""

    oldest = time.time() - args.days * 86400 if args.days else None
    stats = backfill_channel(client, args.channel, bot_user_id=bot_user_id,
                             oldest=oldest, max_messages=args.max_messages,
                             limit=args.limit, dry_run=args.dry_run)
    print(f"\nscanned {stats['scanned']} conversation(s), "
          f"{stats['candidates']} looked like decisions, "
          f"{stats['captured']} captured, {stats['skipped_seen']} already done"
          + (" [dry run]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":  # python -m loop.backfill
    raise SystemExit(main())
