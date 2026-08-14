"""One place for the small "ask the model for JSON" calls Loop's deterministic
detectors make — bug extraction, action items, passive memory capture.

These bypass the Strands agent on purpose (MiniMax M3 doesn't reliably call tools
from prompts), so they hit the model's HTTP API directly. They all used to carry
their own copy of the same request/parse boilerplate; it lives here now. Uses the
same env contract as the agent, so it runs on whatever provider is configured.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

log = logging.getLogger("loop.llm")

# Loop's own bot user ids, skipped when picking a human @mention out of a message.
# Self-populated at startup: slack_app.start() calls register_bot_id() with each
# app's auth.test user_id, so this works in ANY workspace (nothing hardcoded).
# LOOP_BOT_USER_IDS (comma-separated) can seed extra ids for odd setups.
BOT_IDS: set[str] = {
    s.strip() for s in os.environ.get("LOOP_BOT_USER_IDS", "").split(",") if s.strip()
}
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def register_bot_id(user_id: str | None) -> None:
    """Record one of our own bot user ids so mention-extraction skips it."""
    if user_id:
        BOT_IDS.add(user_id)


def extract_json(prompt: str, *, max_tokens: int = 256) -> dict | None:
    """Send ``prompt`` to the configured model and parse a single JSON object from
    the reply (tolerant of ```code fences```). Returns the dict, or None on any
    error — callers treat None as "nothing usable"."""
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    last_err: Exception | None = None
    for attempt in range(2):  # one retry — MiniMax handshakes occasionally time out
        try:
            resp = httpx.post(
                f"{base_url.rstrip('/')}/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
                timeout=20,
            )
            resp.raise_for_status()
            content = resp.json()["content"][0]["text"].strip()
            content = re.sub(r"^```[a-z]*\n?|\n?```$", "", content).strip()
            data = json.loads(content)
            return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("extract_json attempt %d failed: %s", attempt + 1, e)
    log.warning("extract_json giving up after retries: %s", last_err)
    return None


def first_human_mention(raw_text: str) -> str:
    """The first non-bot ``<@U…>`` mention in a message, or "" — used to attribute
    a bug/action item to a tagged human."""
    for m in _MENTION_RE.finditer(raw_text):
        if m.group(1) not in BOT_IDS:
            return m.group(0)
    return ""
