"""One place for the small "ask the model for JSON" calls Cairn's deterministic
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

log = logging.getLogger("cairn.llm")

# Cairn's own bot user ids, skipped when picking a human @mention out of a message.
# Self-populated at startup: slack_app.start() calls register_bot_id() with each
# app's auth.test user_id, so this works in ANY workspace (nothing hardcoded).
# CAIRN_BOT_USER_IDS (comma-separated) can seed extra ids for odd setups.
BOT_IDS: set[str] = {
    s.strip() for s in os.environ.get("CAIRN_BOT_USER_IDS", "").split(",") if s.strip()
}
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def register_bot_id(user_id: str | None) -> None:
    """Record one of our own bot user ids so mention-extraction skips it."""
    if user_id:
        BOT_IDS.add(user_id)


_JSON_SYSTEM = (
    "You return data, not prose. Reply with a single valid JSON object and "
    "nothing else — no explanation, no markdown fences."
)

_json_agent = None


def _temperature() -> float:
    try:
        return max(0.0, float(os.environ.get("CAIRN_EXTRACT_TEMPERATURE", "0")))
    except ValueError:
        return 0.0



def _agent():
    """A tool-less Strands agent used purely for JSON extraction.

    Built through the same provider factory as the main agent, so these calls
    follow CAIRN_MODEL_PROVIDER like everything else. The previous version spoke
    Anthropic's HTTP wire format directly; that silently only worked while the
    provider happened to be Anthropic-compatible, and every capture broke with a
    401 the moment the project moved to Gemini.

    CAIRN_EXTRACT_MODEL routes these to a cheaper/faster model than the one
    answering in Slack — they are small, mechanical calls.
    """
    global _json_agent
    if _json_agent is None:
        from strands import Agent  # noqa: PLC0415

        from cairn.agent import _build_model  # noqa: PLC0415 (avoids an import cycle)

        model = _build_model(os.environ.get("CAIRN_EXTRACT_MODEL") or None)
        # Temperature 0: always take the most likely token instead of sampling.
        # These are classification calls, not writing — creativity here shows up
        # as the same question getting different answers on different days
        # (compacting the live store twice gave opposite verdicts on one pair).
        # Not a hard guarantee (batching and float ordering still vary), but it
        # removes most of the variance.
        # Strands models keep provider params inside their config, not on the
        # instance, and expose get_config()/update_config() — so merge rather than
        # assign, or the factory's max-token cap is silently dropped.
        try:
            params = dict((model.get_config() or {}).get("params") or {})
            params["temperature"] = _temperature()
            model.update_config(params=params)
        except Exception:  # noqa: BLE001 - a provider that does not accept it
            log.debug("could not set temperature on %s", type(model).__name__)
        _json_agent = Agent(
            model=model,
            system_prompt=_JSON_SYSTEM,
            tools=[],
            callback_handler=None,   # these are data calls; nothing to stream to stdout
        )
    return _json_agent


def _parse_json(text: str) -> dict | None:
    """Parse one JSON object out of a model reply, tolerating fences and prose."""
    text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text.strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Some models wrap the object in a sentence; take the outermost braces.
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def extract_json(prompt: str, *, max_tokens: int = 256) -> dict | None:
    """Ask the configured model for a JSON object and parse it.

    Returns the dict, or None on any error — callers treat None as "nothing
    usable", so a failure here must never propagate into the message path.
    ``max_tokens`` is advisory: the cap comes from the provider factory.
    """
    last_err: Exception | None = None
    for attempt in range(2):        # one retry — handshakes occasionally time out
        try:
            agent = _agent()
            agent.messages = []     # these calls are one-shot, never a conversation
            data = _parse_json(str(agent(prompt)))
            if data is not None:
                return data
            log.warning("extract_json attempt %d: reply was not a JSON object", attempt + 1)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("extract_json attempt %d failed: %s", attempt + 1, e)
    if last_err:
        log.warning("extract_json giving up after retries: %s", last_err)
    return None


def first_human_mention(raw_text: str) -> str:
    """The first non-bot ``<@U…>`` mention in a message, or "" — used to attribute
    a bug/action item to a tagged human."""
    for m in _MENTION_RE.finditer(raw_text):
        if m.group(1) not in BOT_IDS:
            return m.group(0)
    return ""
