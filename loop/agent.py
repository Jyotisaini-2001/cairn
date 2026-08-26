"""Loop's Strands Agent.

Built per the official Strands Agents README (sdk-python):

    from strands import Agent
    from strands.models.anthropic import AnthropicModel
    from strands_tools import slack, slack_send_message, calculator, think
    from strands.tools.mcp import MCPClient
    from mcp import stdio_client, StdioServerParameters

    with MCPClient(lambda: stdio_client(StdioServerParameters(command=..., args=[...]))) as mcp:
        agent = Agent(model=..., tools=[...], memory_manager=...)
        response = agent("...")

We follow the documented pattern: the MCPClient owns its stdio connection's
lifetime, so we enter its context before the agent runs and exit after. The
agent itself is built lazily on first use (cheap — just config wiring).
"""
from __future__ import annotations

import contextlib
import logging
import os
import random
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.memory import InjectionFormatContext, MemoryInjectionConfig, MemoryManager
from strands.models.anthropic import AnthropicModel
from strands.tools.mcp import MCPClient
from strands_tools import calculator, slack, think
from strands_tools.slack import slack_send_message

from loop.jira_tool import jira_create_issue, jira_search_issues, jira_stage_ticket

from loop import context as reqctx
from loop.guardrails import Guardrails
from loop.storage import SqliteMemoryStore
from loop.tracing import LoopTracer, reasoning_callback

log = logging.getLogger("loop.agent")

_agents: dict[str, Agent] = {}


@dataclass
class AgentRun:
    """Result of one agent invocation: the reply text plus the telemetry the
    observability layer records (tokens, tools used, model, reasoning, etc.)."""

    text: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: list[str] = field(default_factory=list)
    model_calls: int = 0
    guardrail_hits: int = 0
    reasoning: str = ""
    stop_reason: str | None = None


_DEFAULT_MODEL = "claude-sonnet-4-6"


def _model_id() -> str:
    """The model id, provider-neutral. ``CAIRN_MODEL`` wins; ``ANTHROPIC_MODEL`` is
    kept as the back-compat fallback (MiniMax M3 sets it today)."""
    return os.environ.get("CAIRN_MODEL") or os.environ.get("ANTHROPIC_MODEL", _DEFAULT_MODEL)


def _max_tokens() -> int:
    return int(os.environ.get("CAIRN_MAX_TOKENS", "2048"))


def _thinking_params() -> dict[str, Any]:
    """Anthropic-wire ``thinking`` request param, or {} when disabled.

    MiniMax M3 (and native Claude) only return their reasoning as ``thinking``
    content blocks when the request asks for them — without this param the model
    thinks silently and the `reasoning` telemetry column stays empty. Budget via
    ``CAIRN_THINKING_BUDGET`` (tokens, default 1024; must stay below max_tokens);
    set it to ``off`` if a provider rejects the param."""
    raw = os.environ.get("CAIRN_THINKING_BUDGET", "1024").strip().lower()
    if raw in {"off", "false", "no", "0", ""}:
        return {}
    try:
        budget = int(raw)
    except ValueError:
        budget = 1024
    budget = min(budget, _max_tokens() - 512)
    if budget <= 0:
        return {}
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


def _openai_style_client_args() -> dict[str, Any]:
    """Client args for OpenAI-format clients (OpenAI/LiteLLM). Optional — LiteLLM
    also reads provider keys straight from the environment."""
    args: dict[str, Any] = {}
    if key := (os.environ.get("OPENAI_API_KEY") or os.environ.get("CAIRN_LLM_API_KEY")):
        args["api_key"] = key
    if base := (os.environ.get("OPENAI_BASE_URL") or os.environ.get("CAIRN_LLM_BASE_URL")):
        args["base_url"] = base
    return args


def _build_model(model_id: str | None = None) -> Any:
    """Construct the Strands model for the configured provider.

    This is Loop's USP made real: the provider is **env-selected** rather than
    hardcoded, so the same agent can run on Anthropic, MiniMax, OpenAI, Bedrock,
    a LiteLLM multi-provider gateway, or a local Ollama model — no vendor lock-in.
    ``CAIRN_MODEL_PROVIDER`` picks the backend (default ``anthropic``, which keeps
    the existing MiniMax-M3-via-Anthropic-compatible-endpoint path unchanged).
    Provider SDKs beyond ``anthropic`` are lazy-imported so they're only required
    when actually selected (install via ``pip install "loop[providers]"``).

    ``model_id`` overrides the env model id — used by the ambient loop for cheap
    background scanning (``CAIRN_AMBIENT_MODEL``).
    """
    provider = os.environ.get("CAIRN_MODEL_PROVIDER", "anthropic").strip().lower()
    model_id = model_id or _model_id()
    max_tokens = _max_tokens()

    # Anthropic SDK — also serves any Anthropic-wire-compatible endpoint
    # (MiniMax M3, native Claude, compatible proxies) via ANTHROPIC_BASE_URL.
    if provider in {"anthropic", "minimax", ""}:
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN must be set")
        client_args: dict[str, Any] = {"api_key": api_key}
        if base_url := os.environ.get("ANTHROPIC_BASE_URL"):
            client_args["base_url"] = base_url
        return AnthropicModel(
            client_args=client_args, model_id=model_id, max_tokens=max_tokens,
            params=_thinking_params() or None,
        )

    # LiteLLM gateway — one class, many providers (model_id like "openai/gpt-4o"
    # or "anthropic/claude-3-7-sonnet"). The cleanest multi-provider story.
    if provider == "litellm":
        from strands.models.litellm import LiteLLMModel  # noqa: PLC0415

        return LiteLLMModel(
            client_args=_openai_style_client_args() or None,
            model_id=model_id,
            params={"max_tokens": max_tokens},
        )

    if provider == "openai":
        from strands.models.openai import OpenAIModel  # noqa: PLC0415

        return OpenAIModel(
            client_args=_openai_style_client_args() or None,
            model_id=model_id,
            params={"max_tokens": max_tokens},
        )

    # AWS Bedrock — region-resident inference (boto3 already available).
    if provider == "bedrock":
        from strands.models.bedrock import BedrockModel  # noqa: PLC0415

        kwargs: dict[str, Any] = {"model_id": model_id, "max_tokens": max_tokens}
        if region := (os.environ.get("CAIRN_BEDROCK_REGION") or os.environ.get("AWS_REGION")):
            kwargs["region_name"] = region
        return BedrockModel(**kwargs)

    # Local / self-hosted models via Ollama — fully sovereign, no external calls.
    if provider == "ollama":
        from strands.models.ollama import OllamaModel  # noqa: PLC0415

        host = os.environ.get("CAIRN_OLLAMA_HOST", "http://localhost:11434")
        return OllamaModel(host=host, model_id=model_id, max_tokens=max_tokens)

    if provider == "gemini":
        from strands.models.gemini import GeminiModel  # noqa: PLC0415

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        # Gemini's GenerateContentConfig names the cap `max_output_tokens`;
        # passing `max_tokens` (the Anthropic/OpenAI spelling) is rejected.
        return GeminiModel(
            client_args={"api_key": api_key} if api_key else None,
            model_id=model_id,
            params={"max_output_tokens": max_tokens},
        )

    raise RuntimeError(
        f"Unknown CAIRN_MODEL_PROVIDER={provider!r}. "
        "Supported: anthropic (default), litellm, openai, bedrock, ollama, gemini."
    )


SYSTEM_PROMPT = """You are Cairn — a Slack-native AI agent for teams.

Your job is your team's memory: decisions get made in Slack and then lost in
Slack, and you make them findable again. A cairn is the stack of stones hikers
leave on a trail so the next person finds the path — that is what you are for.
When you answer from memory, always say where it came from (who, which channel,
when); an answer without its source is a rumour, not evidence.

## Jira
When anyone describes a bug, error, crash, or broken feature, call
jira_stage_ticket immediately (summary, description, issue_type="Bug",
priority="High" if users are blocked else "Medium") and output ONLY the text the
tool returns. Don't ask clarifying questions first.

When filing (jira_create_issue), resolve details yourself — never bounce a
question back:
- Assignee: pass a named person as `assignee_name` ("Jyoti") and nothing else —
  the tool looks up their email. Never pass the *requester's* `slack_mention`.
- Project: pass whatever the user named as `project_key`; the tool matches it and
  falls back to the default, so never ask "which project?".

Personality:
- Concise but warm
- Honest — if you don't know or can't find something, say so
- Proactive — surface relevant memory when it adds context

Tools you have:
- slack + slack_send_message — talk to Slack (any Web API method)
- search_memory / add_memory — long-term memory (auto-injected before each reply)
- calculator — math
- jira_stage_ticket — stage a Jira ticket for confirmation (CALL THIS for bugs)
- jira_create_issue — create a Jira issue directly (skip confirmation)
- jira_search_issues — search/list Jira issues; pass "bugs" to list all open bugs, "recent"/"all" to list everything, or a text phrase to search
- fetch (MCP) — fetch any public URL and return its content as text; use this for live pricing, docs, news, or any web lookup

When the user asks a question:
- "tag @alice" → use slack to look up the user, then post a message
- "what did we decide about X" → long-term memory (auto-injected) usually has it
- "summarize this channel" → use slack with conversations_history / conversations_replies
- "remember this" / "save this decision" → call add_memory
- a message with attached files → their contents (images, text, or code) are
  included with the message — read them directly and answer about them
- greetings, thanks, small talk → reply conversationally, no tools
- bug / error / crash / broken feature → call jira_stage_ticket (see rule above)
- pricing / docs / news / "latest X" / any live web question → call fetch with the relevant URL; if you don't know the URL, fetch a search engine results page (e.g. https://duckduckgo.com/html/?q=minimax+m3+pricing)

Memory discipline — you are the filter, so save signal, not noise:
- DO save with add_memory: decisions ("we're going with X"), durable facts
  (schedules, owners, links, config), team/user preferences, commitments, and
  the distilled outcome of an important discussion (an "episode": a one-to-three
  sentence summary of what was decided/learned, not the raw transcript).
- DON'T save: greetings, thanks, chit-chat, one-off lookups, or anything already
  obvious. When in doubt, prefer NOT saving — a noisy memory hurts recall.
- When you're only ANSWERING a recall/lookup question (the fact is already in the
  injected <memory>), just answer — do NOT call add_memory to re-save it.
- Write each memory as a self-contained statement (who/what, and any specifics)
  so it still makes sense months later. Provenance — who said it, the channel,
  and the date — is attached automatically; you don't need to add it yourself.
- Injected memories arrive in a <memory> block tagged with that provenance; cite
  it when it matters ("per @alice on 2026-06-20 …").

Respond in Slack mrkdwn (*bold*, _italic_, `code`, • bullets, > quotes). Keep replies under 1500 chars when possible. Don't announce tool calls — use them and respond naturally.

Reply with the final answer ONLY. Do NOT narrate your private step-by-step
deliberation ("Wait — let me check…", "Actually, looking again…", "Hmm, but…")
in the message the user sees; keep that reasoning internal."""


def _mcp_clients(app_name: str | None = None) -> list[MCPClient]:
    """Build MCPClient instances from env.

    Reads two env vars (per-app overrides the global):
      CAIRN_MCP_SERVERS            — shared across all apps
      CAIRN_MCP_SERVERS_<APP>      — additional servers for one specific app

    Each var is a semicolon-separated list of shell commands, e.g.:
      CAIRN_MCP_SERVERS=uvx mcp-server-fetch;uvx mcp-atlassian --transport stdio

    Returns one MCPClient per entry (empty list if nothing configured).
    """
    specs: list[str] = []
    global_spec = os.environ.get("CAIRN_MCP_SERVERS", "")
    if global_spec.strip():
        specs.extend(s.strip() for s in global_spec.split(";") if s.strip())
    if app_name:
        app_spec = os.environ.get(f"CAIRN_MCP_SERVERS_{app_name.upper()}", "")
        if app_spec.strip():
            specs.extend(s.strip() for s in app_spec.split(";") if s.strip())

    clients: list[MCPClient] = []
    env = os.environ.copy()
    for spec in specs:
        parts = shlex.split(spec)
        command, args = parts[0], parts[1:]
        log.info("loop: MCP server registered (command=%s args=%s)", command, args)
        # Capture command/args in closure to avoid late-binding over loop variable
        clients.append(
            MCPClient(lambda c=command, a=args: stdio_client(StdioServerParameters(command=c, args=a, env=env)))
        )
    return clients


# ---------------------------------------------------------------- tool toggles
# Every tool's schema is re-sent to the model on EVERY request, so an unused tool
# is a permanent tax on latency and cost (`think` alone is ~2.1k tokens). Rather
# than delete capabilities, each is a switch: turn one on the day you need it.
#
#   CAIRN_TOOLS=slack,jira,calculator      (the default)
#   CAIRN_TOOLS=slack,jira,calculator,think
#   CAIRN_TOOLS=slack                      (leanest: memory + Slack only)
#   CAIRN_TOOLS=none                       (memory only)
#
# The memory tools (add_memory / search_memory) are always on — they come from
# the MemoryManager, and they are the product.
_TOOL_GROUPS: dict[str, Any] = {
    "slack": lambda: [slack, slack_send_message],
    "jira": lambda: [jira_stage_ticket, jira_create_issue, jira_search_issues],
    "calculator": lambda: [calculator],
    "think": lambda: [think],
}
_DEFAULT_TOOLS = "slack,jira,calculator"


def _enabled_tools() -> list[Any]:
    """Build the tool list from ``CAIRN_TOOLS`` (comma-separated group names).

    Unknown names are logged and skipped rather than raising, so a typo degrades
    to a missing tool instead of a dead bot."""
    raw = os.environ.get("CAIRN_TOOLS", _DEFAULT_TOOLS).strip()
    if raw.lower() in {"none", "off", ""}:
        names: list[str] = []
    else:
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]

    tools: list[Any] = []
    for name in names:
        group = _TOOL_GROUPS.get(name)
        if group is None:
            log.warning("CAIRN_TOOLS: unknown tool group %r (known: %s)",
                        name, ", ".join(sorted(_TOOL_GROUPS)))
            continue
        tools.extend(group())
    log.info("tools enabled: %s", ", ".join(names) or "(memory only)")
    return tools


def _system_prompt(app_name: str | None) -> str:
    """Base persona, plus an optional per-app override from CAIRN_PERSONA_<NAME>.

    This is the seam for giving each Slack app its own behavior: set e.g.
    CAIRN_PERSONA_RASPUTIN="You are the on-call incident assistant ..." and only
    the `rasputin` app's agent picks it up. Unset → the shared base persona.
    """
    if app_name:
        persona = os.environ.get(f"CAIRN_PERSONA_{app_name.upper()}")
        if persona and persona.strip():
            return f"{SYSTEM_PROMPT}\n\n## This app's specific role\n{persona.strip()}"
    return SYSTEM_PROMPT


def _xml_escape(value: Any) -> str:
    s = "" if value is None else str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _format_memories(ctx: InjectionFormatContext) -> str:
    """Render recalled memories with their provenance so the model can weigh and
    cite them. The default Strands format drops entry metadata; we surface who
    said it, where, and when as attributes on each <entry>."""
    lines: list[str] = []
    for e in ctx.entries:
        meta = e.metadata or {}
        attrs = ""
        if meta.get("author"):
            attrs += f' from="@{_xml_escape(meta["author"])}"'
        if meta.get("channel"):
            attrs += f' in="#{_xml_escape(meta["channel"])}"'
        if meta.get("date"):
            attrs += f' when="{_xml_escape(meta["date"])}"'
        lines.append(f"<entry{attrs}>{_xml_escape(e.content)}</entry>")
    body = "\n".join(lines)
    return f"<memory>\n{body}\n</memory>" if body else ""


def _memory_store() -> Any:
    """Pick the memory backend from env.

    Default ``sqlite`` = the FTS5 + provenance store (no deps, no key). ``hybrid``
    (aliases: ``libsql``/``turso``/``vector``) = the libSQL/Turso hybrid store
    (FTS5 + semantic vectors, RRF-fused); needs ``pip install "loop[vector]"`` and
    raises a clear error if missing, so the choice is explicit.
    """
    backend = os.environ.get("CAIRN_MEMORY_BACKEND", "sqlite").strip().lower()
    if backend in {"hybrid", "libsql", "turso", "vector"}:
        from loop.vector_store import HybridMemoryStore  # noqa: PLC0415 (optional path)

        log.info("memory backend: libsql/turso hybrid (FTS5 + semantic vectors, RRF-fused)")
        return HybridMemoryStore()
    return SqliteMemoryStore()


_direct_store: Any = None


def remember(content: str, *, context: dict[str, Any] | None = None,
             app_name: str | None = None) -> int:
    """Persist a memory directly, bypassing the agent's ``add_memory`` tool.

    MiniMax M3 sometimes replies "Saved …" without actually invoking the tool, so
    explicit "remember …" commands are written straight through the store — using
    the same provenance plumbing as :func:`run` (a ``RequestState`` made current so
    who/where is stamped). Returns the row id."""
    import asyncio  # noqa: PLC0415

    global _direct_store
    if _direct_store is None:
        _direct_store = _memory_store()
    state = reqctx.RequestState()
    _apply_provenance(state, context, app_name)
    token = reqctx.set_current(state)
    try:
        return asyncio.run(_direct_store.add(content))
    finally:
        reqctx.reset_current(token)


# ── Resilience ───────────────────────────────────────────────────────────────
# A provider hiccup used to reach the user as silence: MiniMax started returning
# 429s and Cairn simply never replied (the SDK retried twice, gave up, and the
# exception surfaced as a bare stack trace). Transient failures now get bounded
# retries with exponential backoff + jitter, and a caller can tell a transient
# failure from a real one so the reply can say which.
#
#   CAIRN_MODEL_RETRIES=2        extra attempts after the first  (0 = off)
#   CAIRN_MODEL_RETRY_BASE_MS=600  first backoff; doubles each attempt
_RETRYABLE_MARKERS = (
    "429", "rate limit", "rate_limit", "too many requests", "quota",
    "usage limit", "limit reached", "resource exhausted", "resource_exhausted",
    "500", "502", "503", "504", "overloaded", "unavailable", "internal error",
    "timeout", "timed out", "connection reset", "connection error",
    "temporarily", "try again",
)


# Providers often say exactly how long to wait — Gemini returns
# `"retryDelay": "29s"` on a 429, Anthropic/OpenAI use Retry-After. Ignoring it
# and backing off 0.6s for a 30s window just burns the remaining attempts, which
# is precisely what happened on the Gemini free tier (20 req/min).
_RETRY_HINT_RE = re.compile(
    r"(?:retrydelay\"?\s*[:=]\s*\"?|retry-after\"?\s*[:=]\s*\"?|retry in\s+)"
    r"(\d+(?:\.\d+)?)\s*s?\b", re.IGNORECASE)


def _retry_hint_s(err: BaseException) -> float | None:
    """Seconds the provider asked us to wait, if it said so."""
    m = _RETRY_HINT_RE.search(str(err))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _max_backoff_s() -> float:
    """Ceiling on a single wait. A provider hint of 10 minutes is not worth
    holding a Slack turn open for; we give up and say so instead."""
    try:
        return max(1.0, float(os.environ.get("CAIRN_MODEL_RETRY_MAX_S", "45")))
    except ValueError:
        return 45.0


class TransientModelError(RuntimeError):
    """The model call failed in a way that is worth retrying later.

    Raised after retries are exhausted so the Slack layer can say "busy, try
    again" instead of showing a stack trace."""


class QuotaExhaustedError(TransientModelError):
    """The account's quota is spent — a longer wait will not help today.

    Subclasses TransientModelError so existing handling keeps working, but the
    Slack layer can say something true ("today's quota") rather than "busy".""" 


# Some 429s are not worth waiting for. A per-minute rate limit clears in seconds;
# a per-DAY quota does not clear today, and Gemini still returns a ~60s
# retryDelay with it. Honouring that burned a minute per case in an eval run
# before failing anyway. Fail fast instead, so the caller sees the real problem.
_EXHAUSTED_MARKERS = ("perday", "per day", "requestsperday", "daily limit",
                      "quota exceeded for metric", "billing")


def _is_exhausted(err: BaseException) -> bool:
    text = f"{err}".lower().replace("_", "").replace("-", "")
    return any(m.replace("_", "").replace("-", "") in text for m in _EXHAUSTED_MARKERS)


def _is_transient(err: BaseException) -> bool:
    """Classify by message text, not exception type: each provider SDK raises its
    own class (anthropic.RateLimitError, google ClientError, httpx timeouts), and
    we deliberately do not import every SDK to catch them."""
    if _is_exhausted(err):
        return False        # waiting cannot help; surface it now
    text = f"{type(err).__name__} {err}".lower()
    return any(m in text for m in _RETRYABLE_MARKERS)


def _retries() -> int:
    try:
        return max(0, int(os.environ.get("CAIRN_MODEL_RETRIES", "2")))
    except ValueError:
        return 2


def _retry_base_s() -> float:
    try:
        return max(0.0, float(os.environ.get("CAIRN_MODEL_RETRY_BASE_MS", "600")) / 1000)
    except ValueError:
        return 0.6


_agent_lock = threading.Lock()


def _invoke(agent: Agent, message: Any) -> Any:
    """Run one agent turn, retrying transient provider failures with backoff.

    A non-transient error (bad request, auth, a bug in a tool) is re-raised
    immediately — retrying those just multiplies the damage.

    **Each turn starts from a clean slate.** A Strands ``Agent`` accumulates
    ``messages`` across calls, and ours is cached per app — so without this reset
    one cached agent would carry every user's turns from every channel into every
    subsequent request: cross-channel leakage, and a prompt that grows without
    bound until it blows the context window. Continuity comes from the *thread*
    context (correctly scoped to one Slack thread) and long-term memory, not from
    a shared in-process transcript. The lock keeps the reset and the turn atomic
    now that Slack events are handled off the socket thread.
    """
    attempts = _retries() + 1
    last: BaseException | None = None
    for i in range(attempts):
        try:
            with _agent_lock:
                agent.messages = []
                return agent(message)
        except BaseException as err:  # noqa: BLE001 - classified below, re-raised
            last = err
            if _is_exhausted(err):
                # Not worth a single retry: this resets on a clock, not a timer.
                raise QuotaExhaustedError(str(err)) from err
            if not _is_transient(err):
                raise
            if i == attempts - 1:
                break
            # Prefer what the provider told us; fall back to exponential backoff.
            hint = _retry_hint_s(err)
            delay = hint if hint is not None else _retry_base_s() * (2 ** i)
            delay = min(delay, _max_backoff_s()) * (1 + random.random() * 0.1)
            log.warning("transient model error (attempt %d/%d), retrying in %.1fs: %s",
                        i + 1, attempts, delay, err)
            time.sleep(delay)
    raise TransientModelError(str(last)) from last


def _build_agent(app_name: str | None = None, model_id: str | None = None) -> Agent:
    """Build an agent for one app. Caller is responsible for holding any MCP context.

    ``model_id`` overrides the env model — used for cost-routed background runs.
    """
    tools: list[Any] = _enabled_tools()

    memory_manager = MemoryManager(
        stores=[_memory_store()],
        search_tool_config=True,
        add_tool_config=True,
        injection=MemoryInjectionConfig(format=_format_memories),
    )

    return Agent(
        model=_build_model(model_id),
        system_prompt=_system_prompt(app_name),
        tools=tools,
        memory_manager=memory_manager,
        hooks=[LoopTracer(), Guardrails()],
        callback_handler=reasoning_callback,
    )


def get_agent(app_name: str | None = None, model_id: str | None = None) -> Agent:
    """Return the cached agent for `app_name` (one agent per Slack app).

    A non-default `model_id` is cached under its own key so cost-routed runs
    (e.g. ambient scanning on a cheaper model) don't evict the primary agent.
    """
    key = app_name or "_default"
    if model_id:
        key = f"{key}#{model_id}"
    agent = _agents.get(key)
    if agent is None:
        agent = _build_agent(app_name, model_id)
        _agents[key] = agent
        log.info("loop agent initialised (app=%s)", key)
    return agent


def run(
    prompt: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    request_id: str | None = None,
    app_name: str | None = None,
    model_id: str | None = None,
) -> AgentRun:
    """Invoke the agent and return its reply plus telemetry (`AgentRun`).

    `context` is the Slack request envelope (user/channel/channel_type). When
    present it is prepended as a small, clearly-marked context line so the agent
    knows *who* is asking and *where* — e.g. enough for its slack tool to tag the
    right user or read the right channel. This is the only "context" plumbing;
    there is still no intent parser or router — the agent remains the only brain.

    A `RequestState` is established for the call (using `request_id` if given, so
    it lines up with the edge `Interaction`) and made the current context, so the
    tracing + guardrail hooks can attribute steps and enforce per-request limits.

    If an MCP client is registered, we enter its context manager so the stdio
    connection is alive for the duration of the call (the documented pattern from
    the Strands README), and MCP tools are added inside that scope.
    """
    message = _build_message(_with_context(prompt, context), attachments)
    state = reqctx.RequestState(request_id=request_id) if request_id else reqctx.RequestState()
    _apply_provenance(state, context, app_name)
    token = reqctx.set_current(state)
    try:
        clients = _mcp_clients(app_name)
        if not clients:
            return _result_to_run(_invoke(get_agent(app_name, model_id), message), state)
        with contextlib.ExitStack() as stack:
            for c in clients:
                stack.enter_context(c)
            tools = list(get_agent(app_name, model_id).tool_registry.registry.values())
            for c in clients:
                tools.extend(c.list_tools_sync())
            agent = _build_agent(app_name, model_id)
            agent.tool_registry.process_tools(tools)
            return _result_to_run(_invoke(agent, message), state)
    finally:
        reqctx.reset_current(token)


def _apply_provenance(state: reqctx.RequestState, context: dict[str, Any] | None, app_name: str | None) -> None:
    """Stamp the request with who/where so memories saved during it carry it."""
    ctx = context or {}
    state.author = ctx.get("user")
    state.channel = ctx.get("channel")
    state.team = ctx.get("team")
    state.thread_ts = ctx.get("thread_ts")
    state.source = app_name


def _with_context(prompt: str, context: dict[str, Any] | None) -> str:
    """Prefix the request with who/where, plus the recent turns of this thread.

    The thread block is what makes a follow-up ("what about the second one?")
    resolvable — long-term memory holds durable facts, not the last three
    messages. It is rendered as plain labelled lines rather than a chat
    transcript so the model cannot mistake it for its own instructions.
    """
    if not context:
        return prompt
    bits: list[str] = []
    if context.get("user"):
        bits.append(f"from Slack user <@{context['user']}>")
    if context.get("channel"):
        bits.append(f"in channel <#{context['channel']}>")
    if context.get("channel_type"):
        bits.append(f"({context['channel_type']})")

    header = f"[context] message {' '.join(bits)}" if bits else ""
    thread = _format_thread(context.get("thread"))
    preamble = "\n\n".join(p for p in (header, thread) if p)
    return f"{preamble}\n\n{prompt}" if preamble else prompt


def _format_thread(turns: Any) -> str:
    """Render prior thread turns for the prompt. ``turns`` is a list of
    ``{"speaker", "text"}`` oldest-first, already bounded by the caller."""
    if not turns:
        return ""
    lines = [f"{t.get('speaker', '?')}: {t.get('text', '')}" for t in turns
             if t.get("text")]
    if not lines:
        return ""
    body = "\n".join(lines)
    return ("[thread so far — earlier messages in this Slack thread, oldest "
            f"first; 'you' is Cairn]\n{body}")


# Slack mimetype → Strands image block format. Anything not listed (text/code/
# json) is inlined as text by _build_message.
_IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _pdf_text(data: bytes) -> str:
    """Extract text from a PDF. Lazy import of pypdf (lightweight, pure-Python)."""
    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:  # noqa: BLE001
        log.exception("pdf text extraction failed")
        return ""


def _build_message(text: str, attachments: list[dict[str, Any]] | None) -> Any:
    """Plain string when there are no files; otherwise a Strands multimodal
    content list ``[{"text": …}, {"image": …}, …]``. Images go to the model
    natively (MiniMax M3 is multimodal); text/code/json files are decoded and
    inlined as text blocks. Anything that won't decode is skipped, not fatal."""
    if not attachments:
        return text
    blocks: list[dict[str, Any]] = [{"text": text}]
    for a in attachments:
        mt, data, name = a.get("mimetype", ""), a.get("bytes", b""), a.get("name", "file")
        fmt = _IMAGE_FORMATS.get(mt)
        if fmt:
            blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
            continue
        if mt == "application/pdf":
            body = _pdf_text(data)
            blocks.append({"text": f"\n--- attached PDF: {name} ---\n{body or '(no extractable text)'}"})
            continue
        try:
            body = data.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            log.info("attachment not decodable as text, skipped: %s (%s)", name, mt)
            continue
        blocks.append({"text": f"\n--- attached file: {name} ---\n{body}"})
    return blocks


# MiniMax-M3 (via the Anthropic-compatible endpoint) streams its chain-of-thought
# as plain text wrapped in <think>…</think> — it never becomes a proper Anthropic
# reasoning block, so `reasoning_callback` never fires and the deliberation leaks
# into the reply. We pull those blocks out here.
_THINK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.IGNORECASE | re.DOTALL)
_OPEN_THINK_RE = re.compile(r"<think(?:ing)?>", re.IGNORECASE)
_CLOSE_THINK_RE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)


def _split_reasoning(text: str) -> tuple[str, str]:
    """Separate <think>…</think> reasoning from the user-facing reply.

    Returns ``(clean_reply, thinking)``. The reply keeps only the final answer;
    the thinking is handed to the observability layer to persist. Also handles a
    dangling block (an unclosed ``<think>`` when ``max_tokens`` truncated the
    close, or a stray ``</think>`` with the open lost upstream)."""
    if not text:
        return text, ""
    thoughts = [m.strip() for m in _THINK_RE.findall(text)]
    clean = _THINK_RE.sub("", text)
    if (m := _OPEN_THINK_RE.search(clean)) is not None:
        # open tag never closed → everything from it onward is reasoning
        thoughts.append(_OPEN_THINK_RE.sub("", clean[m.start():]).strip())
        clean = clean[: m.start()]
    elif (m := _CLOSE_THINK_RE.search(clean)) is not None:
        # stray close tag → everything before it is reasoning
        thoughts.append(clean[: m.start()].strip())
        clean = clean[m.end():]
    thinking = "\n\n".join(t for t in thoughts if t).strip()
    return clean.strip(), thinking


def _result_to_run(result: Any, state: reqctx.RequestState | None = None) -> AgentRun:
    """Pull text + metrics off a Strands AgentResult, folding in the per-request
    counters the hooks accumulated (model calls, ordered tool calls, guardrail
    hits, reasoning)."""
    model_id = _model_id()
    usage: dict[str, Any] = {}
    tool_calls: list[str] = []
    metrics = getattr(result, "metrics", None)
    if metrics is not None:
        acc = getattr(metrics, "accumulated_usage", None)
        if isinstance(acc, dict):
            usage = acc

    # Tool attribution comes from the tracer's per-request state. Never fall back
    # to result.metrics.tool_metrics for this: on a cached agent those metrics are
    # LIFETIME-cumulative, so a no-tool turn would inherit every tool the agent
    # ever used (phantom tools in telemetry, false "forbidden tool" eval failures).
    if state is not None:
        tool_calls = list(state.tool_calls)
    elif metrics is not None and isinstance(getattr(metrics, "tool_metrics", None), dict):
        tool_calls = list(metrics.tool_metrics.keys())

    # Split the model's inline <think> deliberation out of the reply, and merge it
    # with any reasoning the streaming callback captured, for the reasoning column.
    clean_text, inline_thinking = _split_reasoning(_extract_text(result))
    reasoning_parts = [p for p in (
        "\n".join(state.reasoning).strip() if state else "",
        inline_thinking,
    ) if p]

    return AgentRun(
        text=clean_text,
        model_id=model_id,
        input_tokens=int(usage.get("inputTokens", 0) or 0),
        output_tokens=int(usage.get("outputTokens", 0) or 0),
        total_tokens=int(usage.get("totalTokens", 0) or 0),
        tool_calls=tool_calls,
        model_calls=state.model_calls if state else 0,
        guardrail_hits=state.guardrail_hits if state else 0,
        reasoning="\n\n".join(reasoning_parts).strip(),
        stop_reason=getattr(result, "stop_reason", None),
    )


def _extract_text(result: Any) -> str:
    """Final assistant text from a Strands AgentResult.

    Strands' `message` is a dict (`{"content": [{"text": ...}]}`), so attribute
    access (`block.text`) never matches — its own `__str__` is the canonical
    extractor (handles text blocks, citations, structured output). We use that,
    with a manual dict/attr parse as a defensive fallback.
    """
    try:
        text = str(result).strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass

    msg = getattr(result, "message", None)
    if msg is None:
        return ""
    content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, "content", []) or []
    parts: list[str] = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()