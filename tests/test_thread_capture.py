"""Thread-level memory capture.

Capture used to distil a single message — the line that tripped the gate. That
keeps the conclusion ("ok let's go with Postgres") and discards the reasoning
that produced it, which is the part people come back for. These cover distilling
a whole conversation, and the cooldown that stops an active thread being
re-captured on every message.
"""
from __future__ import annotations

import os

import pytest

from loop import memory_capture as mc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CAIRN_MEMORY_CAPTURE", "on")
    mc._recent_captures.clear()
    yield
    mc._recent_captures.clear()


TURNS = [
    {"speaker": "<@U1>", "text": "Mongo's transactions are flaky for billing", "ts": "1"},
    {"speaker": "<@U2>", "text": "Postgres means a migration though", "ts": "2"},
    {"speaker": "<@U1>", "text": "correctness wins, we can eat the migration", "ts": "3"},
    {"speaker": "<@U2>", "text": "ok let's go with Postgres", "ts": "4"},
]


def _capture(monkeypatch, extracted="Chose Postgres over Mongo for billing because "
                                    "Mongo's transactions were flaky; accepted a migration cost."):
    saved = {}
    monkeypatch.setattr(mc, "_extract_from_conversation", lambda turns: extracted)
    monkeypatch.setattr(mc, "_save", lambda c, m: saved.update(content=c, meta=m))
    return saved


# ------------------------------------------------------------------ distilling
def test_saves_the_distilled_conversation(monkeypatch):
    saved = _capture(monkeypatch)
    out = mc.capture_conversation(TURNS, author="U1", channel="C1", source="cairn")
    assert out and "Mongo" in saved["content"]


def test_provenance_is_stamped(monkeypatch):
    saved = _capture(monkeypatch)
    mc.capture_conversation(TURNS, author="U1", channel="C1", team="T1",
                            source="cairn", thread_ts="111.1")
    meta = saved["meta"]
    assert meta["author"] == "U1" and meta["channel"] == "C1"
    assert meta["thread_ts"] == "111.1"      # traceable back to the discussion
    assert meta["kind"] == "decision"


def test_whole_conversation_is_sent_not_just_the_last_line(monkeypatch):
    """The reasoning lives in the earlier turns — the point of the change."""
    seen = {}
    monkeypatch.setattr(mc, "_extract_from_conversation",
                        lambda turns: seen.update(n=len(turns)) or "m")
    monkeypatch.setattr(mc, "_save", lambda c, m: None)
    mc.capture_conversation(TURNS, channel="C1")
    assert seen["n"] == 4


def test_nothing_memorable_saves_nothing(monkeypatch):
    saved = _capture(monkeypatch, extracted=None)
    assert mc.capture_conversation(TURNS, channel="C1") is None
    assert saved == {}


def test_empty_conversation_is_a_no_op(monkeypatch):
    _capture(monkeypatch)
    assert mc.capture_conversation([], channel="C1") is None


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CAIRN_MEMORY_CAPTURE", raising=False)
    _capture(monkeypatch)
    assert mc.capture_conversation(TURNS, channel="C1") is None


def test_extraction_failure_is_swallowed(monkeypatch):
    def boom(_turns):
        raise RuntimeError("model down")
    monkeypatch.setattr(mc, "_extract_from_conversation", boom)
    # capture must never affect the message path
    assert mc.capture_conversation(TURNS, channel="C1") is None


# -------------------------------------------------------------------- cooldown
def test_same_thread_is_not_recaptured(monkeypatch):
    _capture(monkeypatch)
    assert mc.capture_conversation(TURNS, channel="C1", thread_ts="t1")
    assert mc.capture_conversation(TURNS, channel="C1", thread_ts="t1") is None


def test_different_threads_are_independent(monkeypatch):
    _capture(monkeypatch)
    assert mc.capture_conversation(TURNS, channel="C1", thread_ts="t1")
    assert mc.capture_conversation(TURNS, channel="C1", thread_ts="t2")


def test_cooldown_expires(monkeypatch):
    _capture(monkeypatch)
    assert mc.capture_conversation(TURNS, channel="C1", thread_ts="t1")
    real = mc.time.time()
    monkeypatch.setattr(mc.time, "time", lambda: real + mc._THREAD_COOLDOWN_S + 1)
    assert mc.capture_conversation(TURNS, channel="C1", thread_ts="t1")


def test_unthreaded_messages_key_off_the_channel(monkeypatch):
    _capture(monkeypatch)
    assert mc.capture_conversation(TURNS, channel="C1")
    assert mc.capture_conversation(TURNS, channel="C1") is None   # same key
