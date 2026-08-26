"""Strip secrets before anything is written down.

Memories are stored word for word, so whatever was typed is what gets kept —
and a decision is exactly the kind of message that carries a credential:

    "ok let's go with the new provider, key is sk-live-a8f3k2b9c1d4e7f0,
     billing goes to priya.sharma@company.com"

Without this, that key lands in the database, gets embedded into a vector, and
is read back out months later by anyone who asks the right question.

Two rules shape what follows:

* **Redact before embedding, not after.** The vector is derived from the text,
  so redacting afterwards would leave the secret encoded in the embedding.
* **Keep the sentence readable.** A memory is only useful if it still says what
  was decided, so a match is replaced by a labelled placeholder — the reader can
  tell a key was there without the key being there.

Applied at the store, which is the single point every write passes through: the
agent's `add_memory`, the direct `remember()` path, passive capture, and
backfill. Putting it anywhere else would leave one of those uncovered.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("cairn.redact")

# Ordered: the most specific patterns first, so a Slack token is labelled as one
# rather than caught by the generic long-token rule.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Provider keys — prefixes are stable and unambiguous.
    ("api-key", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[bapse]-[A-Za-z0-9-]{10,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL)),
    # Credentials embedded in a URL: postgres://user:password@host
    ("url-password", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")),
    # A key/value that names itself a secret. Bounded to one line so a stray
    # "password:" never swallows the rest of a paragraph.
    ("secret", re.compile(
        r"\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\b"
        r"\s*[:=]\s*[\"']?([^\s\"',;]{6,})[\"']?", re.IGNORECASE)),
    ("card-number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]

# Slack renders user/channel refs as <@U123> / <#C123> — those are ids, not
# contact details, and stripping them would break provenance and mentions.
_SLACK_REF = re.compile(r"<[@#!][A-Z0-9]+(\|[^>]*)?>")


def enabled() -> bool:
    return os.environ.get("CAIRN_REDACT", "on").strip().lower() not in {
        "off", "0", "false", "no"}


def _kinds() -> set[str]:
    """Which categories to strip. ``CAIRN_REDACT_KINDS`` narrows it — some teams
    genuinely want an email address kept ("invoices go to billing@…")."""
    raw = os.environ.get("CAIRN_REDACT_KINDS", "").strip()
    if not raw:
        return {k for k, _ in _PATTERNS}
    return {k.strip().lower() for k in raw.split(",") if k.strip()}


def redact(text: str) -> tuple[str, list[str]]:
    """Return ``(clean_text, kinds_found)``.

    Never raises: a redaction failure must not stop a memory being written — but
    it also must not let the raw text through, so a failure returns the input
    unchanged *and says so* in the log rather than silently.
    """
    if not text or not enabled():
        return text, []
    try:
        kinds = _kinds()
        found: list[str] = []
        out = text
        # Protect Slack refs first: <@U0BS8SSEF8V> otherwise reads as a long
        # token, and losing it would break the citation the memory exists for.
        holds: dict[str, str] = {}
        def _hold(m: re.Match) -> str:
            key = f"\x00SLACKREF{len(holds)}\x00"
            holds[key] = m.group(0)
            return key
        out = _SLACK_REF.sub(_hold, out)

        for kind, pattern in _PATTERNS:
            if kind not in kinds:
                continue
            def _sub(m: re.Match, _k: str = kind) -> str:
                # For a self-naming secret keep the label, replace only the value.
                if _k == "secret" and m.groups():
                    return m.group(0).replace(m.group(1), f"[redacted:{_k}]")
                return f"[redacted:{_k}]"
            out, n = pattern.subn(_sub, out)
            if n:
                found.append(kind)

        for key, original in holds.items():
            out = out.replace(key, original)
        if found:
            log.info("redacted %s before storing", ", ".join(found))
        return out, found
    except Exception:  # noqa: BLE001
        log.exception("redaction failed; storing text unchanged")
        return text, []
