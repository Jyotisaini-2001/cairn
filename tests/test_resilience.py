"""Provider-failure handling (CAIRN_MODEL_RETRIES).

A MiniMax 429 storm once reached the user as silence — the SDK retried twice
internally, gave up, and the exception surfaced as a stack trace with no reply.
These cover the classification + backoff wrapper around one agent turn.
"""
from __future__ import annotations

import os

import pytest

from loop.agent import TransientModelError, _invoke, _is_transient


class Flaky:
    """Agent stub: fails `fail_times` then succeeds."""

    def __init__(self, fail_times: int, err: BaseException):
        self.fail_times, self.err, self.calls = fail_times, err, 0

    def __call__(self, _message):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.err
        return "ok"


def setup_function(_f):
    os.environ["CAIRN_MODEL_RETRIES"] = "2"
    os.environ["CAIRN_MODEL_RETRY_BASE_MS"] = "1"    # keep the suite fast


# ------------------------------------------------------------- classification
@pytest.mark.parametrize("msg", [
    "429 Too Many Requests",
    "Token Plan usage limit reached",          # the real MiniMax wording
    "503 Service Unavailable",
    "model is overloaded",
    "Read timed out",
    "connection reset by peer",
])
def test_transient_errors_are_recognised(msg):
    assert _is_transient(RuntimeError(msg))


@pytest.mark.parametrize("msg", [
    "401 Unauthorized",
    "invalid api key",
    "KeyError: 'summary'",
    "model not found",
])
def test_real_failures_are_not_retried(msg):
    assert not _is_transient(RuntimeError(msg))


# ------------------------------------------------------------------- retrying
def test_recovers_after_a_transient_failure():
    a = Flaky(1, RuntimeError("429 rate limit"))
    assert _invoke(a, "hi") == "ok"
    assert a.calls == 2


def test_gives_up_after_the_configured_attempts():
    a = Flaky(99, RuntimeError("429 rate limit"))
    with pytest.raises(TransientModelError):
        _invoke(a, "hi")
    assert a.calls == 3                     # 1 initial + CAIRN_MODEL_RETRIES


def test_non_transient_error_fails_immediately():
    a = Flaky(99, ValueError("invalid api key"))
    with pytest.raises(ValueError):         # surfaces as itself, not Transient
        _invoke(a, "hi")
    assert a.calls == 1                     # never retried


def test_retries_can_be_switched_off():
    os.environ["CAIRN_MODEL_RETRIES"] = "0"
    a = Flaky(99, RuntimeError("429"))
    with pytest.raises(TransientModelError):
        _invoke(a, "hi")
    assert a.calls == 1


def test_success_path_calls_once():
    a = Flaky(0, RuntimeError("nope"))
    assert _invoke(a, "hi") == "ok"
    assert a.calls == 1


def teardown_module(_m):
    os.environ.pop("CAIRN_MODEL_RETRIES", None)
    os.environ.pop("CAIRN_MODEL_RETRY_BASE_MS", None)


# ------------------------------------------------------- provider retry hints
class Recorder:
    """Agent stub that records how long each retry waited."""

    def __init__(self, err, fail_times=99):
        self.err, self.fail_times, self.calls = err, fail_times, 0

    def __call__(self, _m):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.err
        return "ok"


import pytest as _pytest  # noqa: E402


@_pytest.mark.parametrize("text,expected", [
    ('{"retryDelay": "29s"}', 29.0),          # Gemini's literal shape
    ("Please retry in 30.95s.", 30.95),       # Gemini's prose shape
    ("Retry-After: 12", 12.0),                # Anthropic / OpenAI header
    ("just a 429, no hint", None),
])
def test_provider_retry_hint_is_parsed(text, expected):
    from loop.agent import _retry_hint_s
    assert _retry_hint_s(RuntimeError(text)) == expected


def test_hint_is_preferred_over_exponential_backoff(monkeypatch):
    """Backing off 0.6s for a window the provider says is 29s just burns the
    remaining attempts — which is exactly what happened on Gemini's free tier."""
    from loop import agent as ag

    waited = []
    monkeypatch.setattr(ag.time, "sleep", waited.append)
    os.environ["CAIRN_MODEL_RETRIES"] = "2"
    os.environ["CAIRN_MODEL_RETRY_BASE_MS"] = "600"

    with _pytest.raises(ag.TransientModelError):
        ag._invoke(Recorder(RuntimeError('429 {"retryDelay": "29s"}')), "hi")

    assert waited and all(29 <= w <= 33 for w in waited), waited


def test_absurd_hint_is_capped(monkeypatch):
    from loop import agent as ag

    waited = []
    monkeypatch.setattr(ag.time, "sleep", waited.append)
    monkeypatch.setenv("CAIRN_MODEL_RETRY_MAX_S", "10")
    os.environ["CAIRN_MODEL_RETRIES"] = "1"

    with _pytest.raises(ag.TransientModelError):
        ag._invoke(Recorder(RuntimeError('429 retry in 600s')), "hi")

    assert waited and all(w <= 11 for w in waited), waited
