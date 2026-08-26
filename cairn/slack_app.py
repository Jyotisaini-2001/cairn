"""Slack Bolt app(s).

Cairn can run as one OR several Slack apps in a single process (see `_discover_apps`
+ `start`): each configured app gets its own Bolt App, Socket Mode connection, and
agent persona, but they all share the same code, memory store, and telemetry. So
`@Cairn` and a second app can coexist and even behave differently.

Each app has three handlers — all route straight to that app's Strands agent:
  - `app_mention` : user @mentions Cairn in a channel → run() → reply in thread
  - `message`     : tracks every message Cairn can see (channel id/type/user in
                    the logs); answers it ONLY when it's a direct message (DM).
                    This is also what silences Bolt's "unhandled request" 404s
                    for `message` events.
  - `/loop`       : slash command → run() → respond

No passive ingestion in channels, no intent parsing, no command templates. The
agent is the only brain. Every interaction is wrapped in `observability.record`
so we capture request id, channel, user, model, tokens, tool calls, latency, and
outcome (one JSON log line + a row in the `interactions` table).

The `<@U...>` mention strip is purely text-cleaning (so the agent doesn't see its
own name); it is not parsing intent or routing commands.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from cairn import context as reqctx
from cairn import conversation
from cairn import decisions
from cairn import resurface
from cairn import observability as obs
from cairn.agent import QuotaExhaustedError, TransientModelError
from cairn.agent import run as run_agent, remember as remember_memory
from cairn.jira_tool import (
    has_pending, pop_pending, _create_jira_issue_direct, _pending,
    default_project_key, _dedup_check, resolve_project_key,
    _update_issue_direct,
)
from cairn.action_items import looks_like_action_item, extract_action_item, save_action_item
from cairn.llm import extract_json, first_human_mention
from cairn import memory_capture

# Matches a confirmation *prefix* — the message may carry extra instructions
# after it (e.g. "yes, assign this to ishan"). Only consulted when a staged
# ticket exists for the thread, so an incidental leading "ok"/"sure" is safe.
_YES_RE = re.compile(r"^\s*(yes|yep|yeah|yup|confirm|go ahead|file it|create it|do it|sure|ok|okay)\b", re.IGNORECASE)
# Explicit "remember …" save commands. M3 sometimes replies "Saved" without
# actually calling add_memory, so these are persisted directly (see agent.remember).
_REMEMBER_RE = re.compile(
    r"^\s*(?:please\s+)?(?:remember|note|save|keep in mind|don'?t forget|do not forget|make a note)\b[:,]?\s*(?:that\s+|this\s*[:,]?\s*)?(.+)",
    re.IGNORECASE | re.DOTALL,
)
# Optional assignee override inside the confirmation ("assign this/it to <name>").
_NAME = r"[A-Za-z][\w.\-]*(?:\s+(?!and\b|or\b|but\b|with\b|it\b|please\b|priority\b|then\b)[A-Za-z][\w.\-]*)?"
_ASSIGN_OVERRIDE_RE = re.compile(
    rf"(?:re)?assign(?:ee)?\s+(?:[\w\-]+\s+){{0,3}}?to\s+@?({_NAME})",
    re.IGNORECASE,
)
# Priority override in a confirmation ("make it high priority", "set to urgent").
_PRIORITY_OVERRIDE_RE = re.compile(
    r"\b(highest|high|urgent|critical|blocker|medium|normal|low|lowest|minor|trivial)\s*(?:priority)?\b",
    re.IGNORECASE,
)
_PRIORITY_MAP = {
    "highest": "Highest", "critical": "Highest", "blocker": "Highest", "urgent": "High",
    "high": "High", "medium": "Medium", "normal": "Medium",
    "low": "Low", "minor": "Low", "lowest": "Lowest", "trivial": "Lowest",
}
# Clauses we consume from a confirmation so they don't leak into the description.
_CONFIRM_STRIP_RES = [
    _YES_RE,
    re.compile(r"^\s*[,.;:!]+", ),
    re.compile(rf"\bassign\s+(?:\w+\s+){{0,2}}?to\s+@?{_NAME}", re.IGNORECASE),
    re.compile(r"\b(?:make it|set (?:it )?to|change (?:it )?to|mark (?:it )?as|with|and)\b", re.IGNORECASE),
    _PRIORITY_OVERRIDE_RE,
]


# ── Post-creation edits ─────────────────────────────────────────────────────
# "set KAN-25 to high priority", "assign KAN-12 to ravi", "rename KAN-3 to X",
# "add to KAN-9 description: also on Firefox". Gated by an issue key + an edit cue.
_ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_EDIT_CUE_RE = re.compile(
    r"\b(set|change|update|make|assign|reassign|rename|bump|append|add to|edit|modify|"
    r"escalate|deprioriti[sz]e|title|summary|description|priority|assignee)\b",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"(?:\b(?:title|summary)\b\s*(?:to|as|:)?\s*|\brename\b\s+(?:[A-Z][A-Z0-9]+-\d+\s+)?to\s+)"
    r"[\"'“”]?(.+?)[\"'“”]?\s*$",
    re.IGNORECASE,
)
# append with an explicit "description" keyword (capture after it), else after the key
_DESC_APPEND_KW_RE = re.compile(
    r"\b(?:add to|append(?:\s+to)?)\b.*?\bdescription\b\s*[:]?\s*(.+?)\s*$", re.IGNORECASE,
)
_DESC_APPEND_KEY_RE = re.compile(
    r"\b(?:add to|append(?:\s+to)?)\s+[A-Z][A-Z0-9]+-\d+\b\s*[:]?\s*(.+?)\s*$", re.IGNORECASE,
)
_DESC_REPLACE_RE = re.compile(
    r"\bdescription\b\s*(?:to|as|:)\s*[\"'“”]?(.+?)[\"'“”]?\s*$", re.IGNORECASE,
)


def _parse_issue_update(text: str) -> tuple[str, dict] | None:
    """Parse a post-creation edit command. Returns (issue_key, update_kwargs) or None."""
    key_m = _ISSUE_KEY_RE.search(text)
    if not key_m or not _EDIT_CUE_RE.search(text):
        return None
    key = key_m.group(1).upper()
    updates: dict = {}

    assign = _ASSIGN_OVERRIDE_RE.search(text)
    if assign:
        updates["assignee_name"] = assign.group(1).strip()
    else:
        mention = first_human_mention(text)
        if mention and re.search(r"\b(assign|reassign|assignee)\b", text, re.IGNORECASE):
            updates["slack_mention"] = mention

    pri = _PRIORITY_OVERRIDE_RE.search(text)
    if pri and re.search(r"\b(priority|set|change|make|bump|escalate|deprioriti[sz]e|urgent|critical)\b", text, re.IGNORECASE):
        updates["priority"] = _PRIORITY_MAP[pri.group(1).lower()]

    # description append beats replace beats title (most specific first)
    da = _DESC_APPEND_KW_RE.search(text) or _DESC_APPEND_KEY_RE.search(text)
    dr = _DESC_REPLACE_RE.search(text)
    tm = _TITLE_RE.search(text)
    if da and da.group(1).strip():
        updates["append_description"] = da.group(1).strip()
    elif dr and dr.group(1).strip():
        updates["description"] = dr.group(1).strip()
    elif tm and tm.group(1).strip() and re.search(r"\b(title|summary|rename)\b", text, re.IGNORECASE):
        title = _ISSUE_KEY_RE.sub("", tm.group(1)).strip(" \"'“”:-")
        if title:
            updates["summary"] = title

    return (key, updates) if updates else None


def _confirmation_extra_details(text: str) -> str:
    """Strip the affirmative + assignee/priority clauses from a confirmation,
    leaving any genuine extra description the user tacked on (or '')."""
    leftover = text
    for rx in _CONFIRM_STRIP_RES:
        leftover = rx.sub(" ", leftover)
    leftover = re.sub(r"\s+", " ", leftover).strip(" ,.;:!-")
    return leftover if len(leftover) >= 6 else ""

_BUG_RE = re.compile(
    r"\b(crash(es|ed)?|error|errors|\b500\b|\b404\b|fail(s|ed|ure)?|broken|"
    r"bug|not working|doesn'?t work|returns no results?|can'?t|cannot|"
    r"exception|traceback|null pointer|undefined|throws?|blow(s|ing)? up|"
    r"slow|laggy|lag|hang(s|ing|ed)?|freeze(s|ing)?|froze|unresponsive|"
    r"tim(e|ing)?\s?out|timed out|latency|degraded|memory leak|"
    r"wrong|incorrect|glitch|stuck|won'?t load|not loading|"
    r"does nothing|doesn'?t do anything|stopped working|no longer works?|"
    r"isn'?t working|aren'?t working|won'?t|missing|blank|empty|"
    r"never (?:arrives?|loads?|works?|shows?)|fails? to)\b",
    re.IGNORECASE,
)


def _looks_like_bug(text: str) -> bool:
    return bool(_BUG_RE.search(text))


def _extract_bug_details(text: str, raw_text: str) -> dict:
    """Use a focused LLM call to extract structured bug details as JSON. Falls back
    to the raw text (never raises) so staging still works if extraction fails."""
    slack_mention = first_human_mention(raw_text)
    prompt = (
        f"Extract bug details from this Slack message and return ONLY valid JSON.\n"
        f"Message: {text}\n\n"
        f'Return exactly: {{"summary":"short title under 80 chars","description":"full description",'
        f'"priority":"High or Medium","assignee_name":"bare name of who should own/fix it if stated, else empty",'
        f'"project":"Jira project name or key if the message names one, else empty"}}\n'
        f"priority=High if users are blocked or it is a prod issue, else Medium.\n"
        f"assignee_name is a person's name only (e.g. Jyoti) — never an email or a <@...> id."
    )
    data = extract_json(prompt, max_tokens=256) or {}
    clean = _clean_bug_text(text)  # used when the LLM extraction times out / fails
    return {
        "summary": data.get("summary") or clean[:80],
        "description": data.get("description") or clean,
        "issue_type": "Bug",
        "priority": data.get("priority", "Medium"),
        "slack_mention": slack_mention,
        "assignee_name": data.get("assignee_name", "") or "",
        "project": data.get("project", "") or "",
    }


# Trailing instruction clauses to strip from a raw bug report when the LLM
# extraction is unavailable (so the title isn't "…, assign this task to").
_INSTRUCTION_TAIL_RE = re.compile(
    r"[,.;:\-—\s]*\b(?:and\s+)?(?:please\s+)?"
    r"(?:assign(?:ee)?|reassign|give (?:it|this))\b.*$"
    r"|[,.;:\-—\s]*\b(?:and\s+)?(?:make it|set (?:it )?to|mark (?:it )?as|with)\b\s*"
    r"(?:highest|high|urgent|critical|blocker|medium|normal|low|lowest|minor|trivial)\s*(?:priority)?.*$",
    re.IGNORECASE,
)


def _clean_bug_text(text: str) -> str:
    """Strip trailing 'assign to …' / 'make it high priority' instructions so a
    fallback title/description reads like the actual problem, not the command."""
    cleaned = _INSTRUCTION_TAIL_RE.sub("", text).strip(" ,.;:-—")
    return cleaned or text.strip()

log = logging.getLogger("cairn.slack")

_MENTION_RE = re.compile(r"<@[^>]+>")


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


# File attachments: which mimetypes we hand to the model, and a size cap. Images
# go to the model natively (MiniMax M3 is multimodal); text/code/json files are
# inlined as text; PDFs are extracted to text (pypdf) by the agent.
_FILE_ALLOWED = ("image/", "text/", "application/json", "application/pdf")
_FILE_MAX_BYTES = int(os.environ.get("CAIRN_MAX_FILE_BYTES", str(8 * 1024 * 1024)) or 0)


def _download(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted Slack URL)
        return resp.read()


def _collect_files(event: dict, client) -> list[dict]:
    """Download eligible Slack attachments on this event into {name, mimetype, bytes}.

    Slack file URLs are private — they must be fetched with the bot token in an
    Authorization header (a plain GET returns a login page), which is why this
    needs the `files:read` scope. Oversized / disallowed types are skipped, not
    fatal. Returns [] when there are no files (the common case).
    """
    out: list[dict] = []
    for f in event.get("files") or []:
        name, mt, size = f.get("name") or "file", f.get("mimetype") or "", int(f.get("size") or 0)
        if not mt.startswith(_FILE_ALLOWED):
            log.info("file skipped (type) name=%s mimetype=%s", name, mt)
            continue
        if _FILE_MAX_BYTES and size > _FILE_MAX_BYTES:
            log.info("file skipped (size) name=%s bytes=%d cap=%d", name, size, _FILE_MAX_BYTES)
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        try:
            data = _download(url, client.token)
        except Exception:  # noqa: BLE001
            log.exception("file download failed name=%s", name)
            continue
        log.info("file attached name=%s mimetype=%s bytes=%d", name, mt, len(data))
        out.append({"name": name, "mimetype": mt, "bytes": data})
    return out


def _detect_action_item(
    text: str, raw_text: str, channel: str, thread_ts: str | None, team: str | None, app_name: str,
    client=None,
) -> None:
    """Detect and save action items. Adds a 📋 reaction when one is found so users know it was captured."""
    if os.environ.get("CAIRN_ACTION_ITEMS", "").lower() not in {"on", "true", "1"}:
        return
    if not text or not looks_like_action_item(text):
        return
    try:
        item = extract_action_item(text, raw_text)
        if item:
            save_action_item(item, app=app_name, channel=channel, thread_ts=thread_ts or "", team=team)
            log.info("[%s] action item detected: %r deadline=%s", app_name, item["task"][:60], item.get("deadline_raw"))
            # React with 📋 so users see their commitment was captured
            if client and thread_ts:
                try:
                    client.reactions_add(name="clipboard", channel=channel, timestamp=thread_ts)
                except Exception:
                    pass
    except Exception:
        log.exception("action item detection failed silently")


# ── Off-thread dispatch ──────────────────────────────────────────────────────
# Socket Mode wants the event acknowledged within ~3s, but an agent turn takes
# seconds (and can retry a rate-limited provider on top). Running it inline held
# the socket open past the deadline, so Slack re-delivered the event and the user
# got the same answer twice. We hand the work to a small pool and return
# immediately; the reply still lands in-thread when it is ready.
#
#   CAIRN_ASYNC=off        run inline again (useful when debugging)
#   CAIRN_ASYNC_WORKERS=4  concurrent turns
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _async_enabled() -> bool:
    return os.environ.get("CAIRN_ASYNC", "on").strip().lower() not in {
        "off", "0", "false", "no"}


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            try:
                workers = max(1, int(os.environ.get("CAIRN_ASYNC_WORKERS", "4")))
            except ValueError:
                workers = 4
            _executor = ThreadPoolExecutor(max_workers=workers,
                                           thread_name_prefix="cairn-turn")
            log.info("async dispatch on (%d workers)", workers)
        return _executor


def _dispatch(fn, *args, **kwargs) -> None:
    """Run `fn` off the socket thread so Slack gets its ack immediately.

    Exceptions inside the worker are logged rather than lost — a future nobody
    inspects swallows them silently otherwise. `fn` (`_answer`) already reports
    failures to the user itself.
    """
    if not _async_enabled():
        fn(*args, **kwargs)
        return

    def _run() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("background turn failed")

    _pool().submit(_run)


# ── Conversation context ─────────────────────────────────────────────────────
# The fetch/normalise logic lives in `cairn.conversation` so that context,
# capture and backfill all read a conversation the same way. These stay as thin
# wrappers because the flags are read per-request (tests flip them at runtime).
def _thread_context_enabled() -> bool:
    return conversation._flag("CAIRN_THREAD_CONTEXT")


def _thread_limit() -> int:
    return conversation._int_env("CAIRN_THREAD_CONTEXT_LIMIT", 10)


def _thread_history(client, channel: str, thread_ts: str, current_ts: str,
                    bot_user_id: str) -> list[dict]:
    """Recent turns of this thread, oldest first, excluding the current message."""
    if not _thread_context_enabled():
        return []
    turns = conversation.read_thread(
        client, channel, thread_ts, exclude_ts=current_ts,
        bot_user_id=bot_user_id, limit=_thread_limit())
    log.info("thread context: %d turn(s) (thread_ts=%s current_ts=%s)",
             len(turns), thread_ts, current_ts)
    return turns


def _maybe_resurface(event: dict, say, cfg: "AppConfig") -> None:
    """Post a past decision when a settled question is re-opened.

    In-thread, so it sits beside the question rather than interrupting the
    channel. Silent when the record has nothing that answers it, which is most
    of the time — that is the point.
    """
    note = resurface.check(
        _strip_mentions(event.get("text", "") or ""),
        channel=event.get("channel"), team=event.get("team"), source=cfg.name)
    if not note:
        return
    try:
        say(text=note, thread_ts=event.get("thread_ts") or event.get("ts"))
    except Exception:  # noqa: BLE001
        log.exception("[%s] could not post resurfaced decision", cfg.name)


def _capture_conversation(client, event: dict, cfg: "AppConfig") -> None:
    """Distil the conversation around an observed decision into one memory.

    Reads the thread when the message is in one, else recent channel messages —
    the same reader the agent uses for context, so a captured memory always
    corresponds to a conversation Cairn could also have read.
    """
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    if thread_ts:
        turns = conversation.read_thread(
            client, channel, thread_ts, bot_user_id=cfg.bot_user_id,
            limit=conversation._int_env("CAIRN_CAPTURE_LIMIT", 20))
    else:
        turns = conversation.read_channel(
            client, channel, bot_user_id=cfg.bot_user_id,
            limit=conversation._int_env("CAIRN_CAPTURE_LIMIT", 20))
    if not turns:
        return
    # The triggering message's author is the right attribution here: this runs
    # on the message that tripped the gate, and bot messages never reach it.
    memory_capture.capture_conversation(
        turns, author=event.get("user"), channel=channel,
        team=event.get("team"), source=cfg.name, thread_ts=thread_ts)


def _answer(ix: obs.Interaction, event_ctx: dict, say, client, logger, attachments=None,
            app_name=None, bot_user_id: str = "") -> None:
    """Shared path for mention + DM: react, run the agent under telemetry, reply."""
    channel, thread_ts = ix.channel, ix.thread_ts
    _react(client, channel, thread_ts, "eyes")
    # Recent turns of this thread, so follow-ups ("what about the second one?")
    # resolve. Empty for a fresh top-level mention; never fatal.
    #
    # NB: fetch from the thread's PARENT (`event.thread_ts`, carried on the
    # context envelope), not `ix.thread_ts`. The latter is the message's own ts
    # — deliberately so, since replying with it roots Cairn's answer under the
    # mention — but asking conversations_replies for a *reply's* ts returns only
    # that one message, which is how this silently fetched nothing.
    parent_ts = event_ctx.get("thread_ts") or thread_ts
    event_ctx = {**event_ctx, "thread": conversation.read_context(
        client, channel,
        thread_ts=parent_ts,
        in_thread=bool(event_ctx.get("in_thread")),
        current_ts=event_ctx.get("ts") or "",
        bot_user_id=bot_user_id)}
    try:
        with obs.record(ix):
            run = run_agent(
                ix.prompt, attachments=attachments, context=event_ctx,
                request_id=ix.request_id, app_name=app_name,
            )
            ix.from_run(run)
            say(text=run.text or "_(no response)_", thread_ts=thread_ts)
        _swap(client, channel, thread_ts, "white_check_mark")
    except QuotaExhaustedError as err:
        # Distinct from "busy": this one resets on a clock, so "try again in a
        # moment" would be a lie.
        logger.warning("quota exhausted rid=%s: %s", ix.request_id, str(err)[:200])
        _swap(client, channel, thread_ts, "warning")
        say(text=":warning: I've used up today's model quota — it resets on the "
                 "provider's daily clock.", thread_ts=thread_ts)
    except TransientModelError as err:
        # Provider was rate-limited / overloaded and retries ran out. Say so in
        # words — silence (or a stack trace) is what this used to look like.
        logger.warning("model unavailable rid=%s: %s", ix.request_id, err)
        _swap(client, channel, thread_ts, "warning")
        say(text=":warning: The model is busy or rate-limited right now — "
                 "try me again in a moment.", thread_ts=thread_ts)
    except Exception as err:  # noqa: BLE001
        logger.exception("agent invocation failed rid=%s", ix.request_id)
        _swap(client, channel, thread_ts, "warning")
        say(text=f":warning: Something went wrong: {err}", thread_ts=thread_ts)


@dataclass
class AppConfig:
    """One Slack app Cairn runs as: a short name + its bot/app token pair."""

    name: str
    bot_token: str
    app_token: str
    bot_user_id: str = ""  # filled in at start() from auth.test; used to @-mention correctly


def _discover_apps() -> list[AppConfig]:
    """Find every configured Slack app from the environment.

    Two conventions, both supported so multiple apps coexist:
      • Named pairs (preferred):  SLACK_BOT_TOKEN_<NAME> + SLACK_APP_TOKEN_<NAME>
          e.g. SLACK_BOT_TOKEN_LOOP/SLACK_APP_TOKEN_LOOP and
               SLACK_BOT_TOKEN_RASPUTIN/SLACK_APP_TOKEN_RASPUTIN  → two apps.
      • Bare pair (back-compat):  SLACK_BOT_TOKEN + SLACK_APP_TOKEN  → one app,
          named by SLACK_APP_NAME (default "cairn").
    Each app gets its own Bolt App + Socket Mode connection in `start()`, and its
    own agent persona (CAIRN_PERSONA_<NAME>).
    """
    pairs: dict[str, dict[str, str]] = {}
    for key, val in os.environ.items():
        if not val:
            continue
        if key.startswith("SLACK_BOT_TOKEN_"):
            pairs.setdefault(key[len("SLACK_BOT_TOKEN_"):].lower(), {})["bot"] = val
        elif key.startswith("SLACK_APP_TOKEN_"):
            pairs.setdefault(key[len("SLACK_APP_TOKEN_"):].lower(), {})["app"] = val
    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_APP_TOKEN"):
        name = (os.environ.get("SLACK_APP_NAME") or "cairn").lower()
        slot = pairs.setdefault(name, {})
        slot.setdefault("bot", os.environ["SLACK_BOT_TOKEN"])
        slot.setdefault("app", os.environ["SLACK_APP_TOKEN"])

    configs: list[AppConfig] = []
    for name, toks in pairs.items():
        if toks.get("bot") and toks.get("app"):
            configs.append(AppConfig(name=name, bot_token=toks["bot"], app_token=toks["app"]))
        else:
            missing = "app token" if toks.get("bot") else "bot token"
            log.warning("slack app %r missing its %s — skipping", name, missing)
    return configs


def _build_app(cfg: AppConfig) -> App:
    app = App(token=cfg.bot_token)

    @app.event("app_mention")
    def on_mention(event, say, client, logger):
        attachments = _collect_files(event, client)
        text = _strip_mentions(event.get("text", "") or "")
        if not text:
            text = "Please look at the attached file(s)." if attachments else "hello"
        ix = obs.Interaction(
            entrypoint=f"{cfg.name}:app_mention",
            team=event.get("team"),
            channel=event["channel"],
            channel_type=event.get("channel_type"),
            user=event.get("user"),
            thread_ts=event.get("ts"),
            prompt=text,
        )
        log.info(
            "[%s] mention rid=%s channel=%s user=%s thread=%s files=%d",
            cfg.name, ix.request_id, ix.channel, ix.user, ix.thread_ts, len(attachments),
        )

        raw_text = event.get("text", "") or ""

        # Post-creation edit shortcut: "set KAN-25 to high priority", "assign KAN-12
        # to ravi", "rename KAN-3 to X", "add to KAN-9 description: …". Needs an
        # issue key + an edit cue. Runs before bug-detection so an issue-key edit
        # isn't mistaken for a new bug report.
        if os.environ.get("JIRA_API_TOKEN"):
            parsed = _parse_issue_update(text)
            if parsed:
                key, updates = parsed
                _react(client, ix.channel, ix.thread_ts, "eyes")
                try:
                    result = _update_issue_direct(key, **updates)
                    say(text=f":arrows_counterclockwise: {result}", thread_ts=ix.thread_ts)
                    _swap(client, ix.channel, ix.thread_ts, "white_check_mark")
                except Exception as err:
                    logger.exception("issue update failed rid=%s", ix.request_id)
                    _swap(client, ix.channel, ix.thread_ts, "warning")
                    say(text=f":warning: Couldn't update {key}: {err}", thread_ts=ix.thread_ts)
                return

        # Bug-detection shortcut: if the message looks like a bug report and
        # Jira is configured, stage the ticket directly without going through
        # the agent (M3 doesn't reliably call jira_stage_ticket from prompts).
        thread_ts_for_staging = event.get("ts")
        if _looks_like_bug(text) and os.environ.get("JIRA_API_TOKEN"):
            _react(client, ix.channel, ix.thread_ts, "eyes")
            try:
                ticket = _extract_bug_details(text, raw_text)
                # Dedup: if a similar open ticket already exists, skip staging
                existing = _dedup_check(ticket["summary"])
                if existing:
                    say(
                        text=f":mag: Looks like this may already be tracked: *{existing}* — check Jira before filing a new one.",
                        thread_ts=ix.thread_ts,
                    )
                    _swap(client, ix.channel, ix.thread_ts, "white_check_mark")
                    return
                _pending[thread_ts_for_staging] = ticket
                assignee_display = ticket["slack_mention"] or ticket.get("assignee_name", "")
                assignee_line = f"\n• *Assignee:* {assignee_display}" if assignee_display else ""
                project_key, _ = resolve_project_key(ticket.get("project", ""))
                mention = f"<@{cfg.bot_user_id}>" if cfg.bot_user_id else f"@{cfg.name.capitalize()}"
                msg = (
                    f"Here's what I'll file in Jira:\n"
                    f"• *Title:* {ticket['summary']}\n"
                    f"• *Project:* {project_key} | *Type:* Bug | *Priority:* {ticket['priority']}{assignee_line}\n\n"
                    f"Reply *{mention} yes* to confirm and I'll create it."
                )
                say(text=msg, thread_ts=ix.thread_ts)
                _swap(client, ix.channel, ix.thread_ts, "white_check_mark")
            except Exception as err:
                logger.exception("bug staging failed rid=%s", ix.request_id)
                _swap(client, ix.channel, ix.thread_ts, "warning")
                say(text=f":warning: Something went wrong: {err}", thread_ts=ix.thread_ts)
            return

        # Confirmation shortcut: if the user says "yes" in a thread that has a
        # staged Jira ticket, create it directly without going through the agent.
        parent_ts = event.get("thread_ts") or event.get("ts")
        if _YES_RE.match(text) and has_pending(parent_ts):
            ticket = pop_pending(parent_ts)
            _react(client, ix.channel, ix.thread_ts, "eyes")
            # An assignee named in the confirmation itself overrides whatever was
            # staged (name-first resolution beats the original mention/email).
            assignee_email = ticket.get("assignee_email", "")
            slack_mention = ticket.get("slack_mention", "")
            assignee_name = ticket.get("assignee_name", "")
            override = _ASSIGN_OVERRIDE_RE.search(text)
            if override:
                assignee_name = override.group(1).strip()
                assignee_email = ""   # let name-first resolution take over
                slack_mention = ""
            # Priority override ("make it high priority", "set to urgent").
            priority = ticket.get("priority", "Medium")
            pri = _PRIORITY_OVERRIDE_RE.search(text)
            if pri:
                priority = _PRIORITY_MAP[pri.group(1).lower()]
            # Extra free-text details after the confirmation → append to description.
            extra = _confirmation_extra_details(text)
            description = ticket["description"]
            if extra:
                description = f"{description}\n\nAdded on confirmation: {extra}"
            try:
                result = _create_jira_issue_direct(
                    project_key=ticket.get("project") or default_project_key(),
                    summary=ticket["summary"],
                    description=description,
                    issue_type=ticket.get("issue_type", "Bug"),
                    priority=priority,
                    assignee_email=assignee_email,
                    slack_mention=slack_mention,
                    assignee_name=assignee_name,
                )
                say(text=f":ticket: {result}", thread_ts=ix.thread_ts)
                _swap(client, ix.channel, ix.thread_ts, "white_check_mark")
            except Exception as err:
                logger.exception("jira create failed")
                _swap(client, ix.channel, ix.thread_ts, "warning")
                say(text=f":warning: Failed to create ticket: {err}", thread_ts=ix.thread_ts)
            return

        # Explicit "remember …" shortcut: save directly (M3 fakes the "Saved"
        # reply without calling add_memory), then confirm.
        mem = _REMEMBER_RE.match(text)
        if mem:
            fact = mem.group(1).strip()
            if fact:
                _react(client, ix.channel, ix.thread_ts, "eyes")
                try:
                    remember_memory(fact, context=_ctx(event), app_name=cfg.name)
                    say(text=f":brain: Saved to memory: {fact}", thread_ts=ix.thread_ts)
                    _swap(client, ix.channel, ix.thread_ts, "white_check_mark")
                except Exception as err:
                    logger.exception("memory save failed rid=%s", ix.request_id)
                    _swap(client, ix.channel, ix.thread_ts, "warning")
                    say(text=f":warning: Couldn't save that: {err}", thread_ts=ix.thread_ts)
                return

        # Action-item detection: runs after Jira checks, before the agent.
        _detect_action_item(text, raw_text, ix.channel, event.get("ts"), event.get("team"), cfg.name, client=client)

        _dispatch(_answer, ix, _ctx(event), say, client, logger,
                  attachments=attachments, app_name=cfg.name,
                  bot_user_id=cfg.bot_user_id)

    @app.event("message")
    def on_message(event, say, client, logger):
        # Tracking + DM support. We log EVERY message Cairn receives so each
        # channel it can see is visible in the logs, but we only *answer* DMs.
        # Channel messages stay observe-only — @mention / /loop are how you
        # invoke Cairn in a channel. (Registering this handler is also what stops
        # Bolt's 404 "unhandled request" warnings for `message` events.)
        if event.get("bot_id"):
            return  # ignore our own messages (avoids loops)
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return  # ignore edits/joins/etc., but KEEP file uploads (file_share)
        channel = event.get("channel")
        channel_type = event.get("channel_type")
        user = event.get("user")
        raw = event.get("text", "") or ""
        text = _strip_mentions(raw)
        log.info(
            "[%s] message rid=- channel=%s type=%s user=%s preview=%r",
            cfg.name, channel, channel_type, user, text[:80],
        )
        if channel_type != "im":
            _detect_action_item(text, raw, channel, event.get("ts"), event.get("team"), cfg.name, client=client)
            # Passively capture decisions Cairn only observes (off unless
            # CAIRN_MEMORY_CAPTURE=on), so recall works without an @Cairn turn.
            #
            # The gate runs on the triggering message (cheap, no model call), but
            # what gets distilled is the surrounding CONVERSATION — the decision
            # is the last line of a discussion, and the reasoning that produced it
            # is in the lines before. Off the socket thread: this makes a model
            # call and one or two Slack reads.
            # `event.get("bot_id")` guards the feedback loop: Cairn's own replies
            # are full of decision language ("We decided to use Postgres
            # because…") since it reports decisions back, and capturing those
            # would feed its output into its own memory, to be cited later as
            # evidence. Bolt delivers bot messages here too, so this matters.
            if (not event.get("bot_id")
                    and memory_capture.enabled()
                    and memory_capture.looks_memorable(text)):
                _dispatch(_capture_conversation, client, event, cfg)
            # Proactivity, earned (CAIRN_RESURFACE=off by default): if someone
            # re-opens a question the record already answers, offer the decision
            # and its source. Same engine as recall, pushed instead of pulled.
            elif not event.get("bot_id") and resurface.enabled():
                _dispatch(_maybe_resurface, event, say, cfg)
            return  # observe-only (channel chatter) — not a DM to Cairn
        if "<@" in raw:
            return  # a mention inside a DM: let the app_mention handler take it
        attachments = _collect_files(event, client)
        if not text and not attachments:
            return  # empty DM noise
        ix = obs.Interaction(
            entrypoint=f"{cfg.name}:dm",
            team=event.get("team"),
            channel=channel,
            channel_type=channel_type,
            user=user,
            thread_ts=event.get("ts"),
            prompt=text or "(see attached file)",
        )
        log.info("[%s] dm rid=%s channel=%s user=%s files=%d", cfg.name, ix.request_id, channel, user, len(attachments))
        _dispatch(_answer, ix, _ctx(event), say, client, logger,
                  attachments=attachments, app_name=cfg.name,
                  bot_user_id=cfg.bot_user_id)

    def _slash(ack, respond, command):
        # The slash command answers straight from the decision log when the ask
        # is about the record ("decisions", "what did we decide last week"). That
        # path is deterministic — a listing read from the store, with sources —
        # which costs nothing, cannot hallucinate, and works when the model
        # quota is spent. Anything else goes to the agent.
        ack()
        text = (command.get("text") or "").strip()
        if not text or _LOG_REQUEST_RE.search(text):
            scope = None
            if (cid := command.get("channel_id")):
                scope = reqctx.scope_for(cid, command.get("team_id"))
            respond(decisions.log_decisions(text, scope=scope))
            return
        ix = obs.Interaction(
            entrypoint=f"{cfg.name}:slash",
            team=command.get("team_id"),
            channel=command.get("channel_id"),
            channel_type="slash",
            user=command.get("user_id"),
            prompt=text,
        )
        log.info("[%s] slash rid=%s channel=%s user=%s", cfg.name, ix.request_id, ix.channel, ix.user)
        try:
            with obs.record(ix):
                run = run_agent(text, context=_ctx_cmd(command), request_id=ix.request_id, app_name=cfg.name)
                ix.from_run(run)
                respond(run.text or "_(no response)_")
        except Exception as err:  # noqa: BLE001
            log.exception("[%s] slash command failed rid=%s", cfg.name, ix.request_id)
            respond(f":warning: Something went wrong: {err}")

    # `/cairn` is the name; `/loop` stays registered so an app installed with the
    # old slash command keeps working.
    app.command("/cairn")(_slash)
    app.command("/loop")(_slash)

    if _assistant_enabled():
        _attach_assistant(app, cfg)
    return app


# Asking *about the record* rather than *from* it — these go to the decision log,
# not the agent.
_LOG_REQUEST_RE = re.compile(
    r"\b(decisions?|decision log|what did we (decide|agree)|what have we "
    r"(decided|agreed)|recap|digest|log)\b", re.IGNORECASE)


def _ctx(event: dict) -> dict:
    return {
        "user": event.get("user"),
        "channel": event.get("channel"),
        "channel_type": event.get("channel_type"),
        "team": event.get("team"),
        "thread_ts": event.get("thread_ts") or event.get("ts"),
        "ts": event.get("ts"),
        # threaded reply? (Slack only sends thread_ts when the message is one)
        "in_thread": bool(event.get("thread_ts")),
    }


def _ctx_cmd(command: dict) -> dict:
    return {
        "user": command.get("user_id"),
        "channel": command.get("channel_id"),
        "channel_type": "slash",
        "team": command.get("team_id"),
    }


# ── Native Slack AI "Assistant" surface (Slack AI capabilities) ───────────────
# Opt-in via CAIRN_SLACK_ASSISTANT=on. Requires, on the Slack app: the "Agents & AI
# Apps" feature enabled, the `assistant:write` scope, and the `assistant_thread_*`
# events subscribed. Routes the same single Strands agent into the assistant pane
# (side panel / app DM) with suggested prompts + a live "is thinking…" status.
_SUGGESTED_PROMPTS = [
    {"title": "Recall a decision", "message": "What did we decide about our database?"},
    {"title": "Summarize this channel", "message": "Summarize the latest messages in this channel."},
    {"title": "Remember something", "message": "Remember that our standup is at 10am every weekday."},
]


def _assistant_enabled() -> bool:
    return os.environ.get("CAIRN_SLACK_ASSISTANT", "off").strip().lower() in {"on", "1", "true", "yes"}


def _attach_assistant(app: "App", cfg: "AppConfig") -> None:
    """Wire Bolt's Assistant (AI app pane) to the same agent. Best-effort: if this
    slack_bolt build lacks the Assistant class, log and skip rather than crash."""
    try:
        from slack_bolt import Assistant
    except Exception:  # noqa: BLE001 — older bolt without Assistant
        log.warning("[%s] slack_bolt has no Assistant class; assistant disabled", cfg.name)
        return

    assistant = Assistant()

    @assistant.thread_started
    def _started(say, set_suggested_prompts, logger):
        say(":wave: Hi! I'm *Cairn*. Ask me to recall a decision, summarize a channel, or remember something.")
        try:
            set_suggested_prompts(prompts=_SUGGESTED_PROMPTS, title="Try one of these:")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] set_suggested_prompts failed", cfg.name)

    @assistant.user_message
    def _on_message(payload, client, set_status, say, logger, context):
        text = _strip_mentions(payload.get("text", "") or "")
        attachments = _collect_files(payload, client)
        if not text and not attachments:
            return
        try:
            set_status("is thinking…")
        except Exception:  # noqa: BLE001
            pass
        ix = obs.Interaction(
            entrypoint=f"{cfg.name}:assistant",
            team=context.get("team_id") or payload.get("team"),
            channel=payload.get("channel"),
            channel_type="assistant",
            user=payload.get("user"),
            thread_ts=payload.get("thread_ts"),
            prompt=text or "(see attached file)",
        )
        log.info("[%s] assistant rid=%s channel=%s user=%s files=%d",
                 cfg.name, ix.request_id, ix.channel, ix.user, len(attachments))
        try:
            with obs.record(ix):
                run = run_agent(
                    ix.prompt, attachments=attachments,
                    context={
                        "user": ix.user, "channel": ix.channel, "channel_type": "assistant",
                        "team": ix.team, "thread_ts": ix.thread_ts,
                    },
                    request_id=ix.request_id, app_name=cfg.name,
                )
                ix.from_run(run)
                say(text=run.text or "_(no response)_")
        except Exception as err:  # noqa: BLE001
            logger.exception("[%s] assistant turn failed rid=%s", cfg.name, ix.request_id)
            say(text=f":warning: Something went wrong: {err}")

    app.assistant(assistant)
    log.info("[%s] Slack AI Assistant attached (needs assistant:write + Agents & AI Apps)", cfg.name)


def _react(client, channel: str, ts: str, name: str) -> None:
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001
        pass


def _swap(client, channel: str, ts: str, name: str) -> None:
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
    except Exception:  # noqa: BLE001
        pass
    _react(client, channel, ts, name)


def start() -> None:  # pragma: no cover
    configs = _discover_apps()
    if not configs:
        raise RuntimeError(
            "No Slack apps configured. Set SLACK_BOT_TOKEN + SLACK_APP_TOKEN, and/or "
            "SLACK_BOT_TOKEN_<NAME> + SLACK_APP_TOKEN_<NAME> pairs."
        )
    from cairn.llm import register_bot_id

    for cfg in configs:
        app = _build_app(cfg)
        try:
            auth = app.client.auth_test()
            cfg.bot_user_id = auth.get("user_id") or ""
            register_bot_id(auth.get("user_id"))
            log.info("cairn: app %r is bot user %s (%s)", cfg.name, auth.get("user_id"), auth.get("user"))
        except Exception:  # noqa: BLE001 — identity lookup is best-effort
            log.warning("cairn: auth.test failed for app %r; bot-mention filtering may miss it", cfg.name)
        handler = SocketModeHandler(app, cfg.app_token)
        handler.connect()  # non-blocking: opens this app's socket on a background thread
        log.info("cairn: Slack app %r connected (socket mode)", cfg.name)
    log.info("cairn: %d app(s) running: %s", len(configs), ", ".join(c.name for c in configs))
    # Ambient (proactive PM) mode — off unless CAIRN_AMBIENT=on; one daemon per app.
    from cairn import ambient  # noqa: PLC0415 (optional path; keeps import cost off the default)

    ambient.maybe_start(configs)
    threading.Event().wait()  # block forever; each app runs in its own background thread
