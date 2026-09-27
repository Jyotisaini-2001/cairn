"""Jev — TypeSafe AI's System One model — for Cairn's typed judgements.

Some of what Cairn asks a model is not writing but a multiple-choice question:
"is this new decision a duplicate of the old one, does it replace it, or are they
independent?" An LLM answers that by generating text we then parse, and it
samples: compacting the live store twice gave opposite verdicts on one pair.

Jev answers typed questions directly — a choice plus a calibrated probability
over every option and a confidence — with no sampling and no text to parse. So
the same pair gets the same verdict, and the confidence gives "unclear → keep
both" a number instead of a hope.

Opt-in and fail-open: ``CAIRN_CLASSIFIER=jev`` plus ``TYPESAFE_API_KEY``. Any
failure (no key, network, a malformed answer) returns None and the caller falls
back to the configured LLM, so turning Jev on can never make Cairn worse than it
was. Plain HTTP (the same urllib pattern as the hosted embedders) — no SDK.

    TYPESAFE_API_KEY           required to enable
    CAIRN_CLASSIFIER=jev       route typed judgements to Jev (default: llm)
    CAIRN_JEV_MODEL            default jev-latest; pin e.g. jev-1.13.0
    CAIRN_JEV_TIMEOUT          seconds per request, default 10
    TYPESAFE_BASE_URL          default https://api.typesafe.ai/v1
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("cairn.jev")

_DEFAULT_BASE = "https://api.typesafe.ai/v1"
# 429 rate-limited, 529 overloaded, 5xx transient: worth one retry. Anything else
# (401 bad key, 422 bad request) will fail the same way again.
_RETRY_STATUS = {429, 500, 502, 503, 504, 529}


@dataclass(frozen=True)
class Choice:
    choice: str
    confidence: float
    probabilities: dict[str, float]
    # Answers to any yes/no checks asked alongside, as P(yes). A check Jev did
    # not answer is absent, so callers can tell "no" from "unknown".
    checks: dict[str, float] = field(default_factory=dict)


def enabled() -> bool:
    return (os.environ.get("CAIRN_CLASSIFIER", "llm").strip().lower() == "jev"
            and bool(os.environ.get("TYPESAFE_API_KEY", "").strip()))


def _model() -> str:
    return os.environ.get("CAIRN_JEV_MODEL", "jev-latest").strip() or "jev-latest"


def _timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("CAIRN_JEV_TIMEOUT", "10")))
    except ValueError:
        return 10.0


def _post(payload: dict) -> dict:
    base = os.environ.get("TYPESAFE_BASE_URL", _DEFAULT_BASE).rstrip("/")
    req = urllib.request.Request(
        f"{base}/systemone",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY'].strip()}"},
    )
    with urllib.request.urlopen(req, timeout=_timeout()) as resp:  # noqa: S310 (configured host)
        return json.loads(resp.read())


def _confidence(answer: dict, n_options: int) -> float:
    """The API's confidence, or the same measure derived from the probabilities
    (how concentrated they are on one option: 0 = uniform, 1 = certain)."""
    conf = answer.get("confidence")
    if isinstance(conf, (int, float)):
        return float(conf)
    probs = answer.get("probabilities") or {}
    if not probs or n_options < 2:
        return 0.0
    top = max(float(p) for p in probs.values())
    return max(0.0, (n_options * top - 1) / (n_options - 1))


def choice(state: str | dict, instructions: str, criteria: dict[str, str],
           checks: dict[str, str | dict] | None = None) -> Choice | None:
    """Ask Jev to pick one of ``criteria`` (option -> what it means) for ``state``.

    ``checks`` (name -> statement, or name -> {"instructions", "criteria":
    {"true", "false"}}) are yes/no questions answered in the same call — same
    price, same latency. TypeSafe's own guidance for Jev's literal
    reading is to ask about the exact condition directly rather than hope one
    broad question covers it.

    Returns None on any failure — callers fall back to the LLM path.
    """
    questions = {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}}
    for name, spec in (checks or {}).items():
        spec = spec if isinstance(spec, dict) else {"instructions": spec}
        questions[name] = {"type": "noul", **spec}
    payload = {"model": _model(), "state": state, "questions": questions}
    started = time.perf_counter()
    for attempt in range(2):
        try:
            data = _post(payload)
            break
        except urllib.error.HTTPError as e:
            if e.code in _RETRY_STATUS and attempt == 0:
                time.sleep(1.0)
                continue
            log.warning("jev request failed: HTTP %s", e.code)
            return None
        except Exception as e:  # noqa: BLE001 - network, timeout, bad JSON
            if attempt == 0:
                time.sleep(1.0)
                continue
            log.warning("jev request failed: %s", e)
            return None

    answers = (data or {}).get("answers") or {}
    answer = answers.get("q") or {}
    picked = answer.get("choice")
    if picked not in criteria:
        log.warning("jev returned no usable choice: %r", answer)
        return None
    result = Choice(
        choice=picked,
        confidence=_confidence(answer, len(criteria)),
        probabilities={k: float(v) for k, v in (answer.get("probabilities") or {}).items()},
        checks={name: float(answers[name]["noul"]) for name in (checks or {})
                if isinstance((answers.get(name) or {}).get("noul"), (int, float))},
    )
    usage = data.get("usage") or {}
    extra = "".join(f" {k}={v:.2f}" for k, v in result.checks.items())
    log.info("jev model=%s choice=%s confidence=%.2f%s tokens=%s latency_ms=%d",
             data.get("model", _model()), result.choice, result.confidence, extra,
             usage.get("input_tokens", "?"), (time.perf_counter() - started) * 1000)
    return result
