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

from cairn import memory_capture as mc


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
    monkeypatch.setattr(mc, "_extract_from_conversation", lambda turns, **k: extracted)
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
                        lambda turns, **k: seen.update(n=len(turns)) or "m")
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


# ------------------------------------------------------- focus on the trigger
# Outside a thread the "conversation" is the last N channel messages — several
# topics and some questions. Asked whether that reached a decision, the model
# said no, and a decision posted after two unrelated questions was dropped live.
CHANNEL = [
    {"speaker": "<@U1>", "text": "we decided on 3-week sprints", "ts": "1"},
    {"speaker": "<@U1>", "text": "how long are our sprints?", "ts": "2"},
    {"speaker": "<@U1>", "text": "what's our hotfix review policy?", "ts": "3"},
    {"speaker": "<@U1>", "text": "we decided design reviews are on Thursdays", "ts": "4"},
]


def _prompt_for(monkeypatch, turns, focus_ts=None, reply=None):
    seen = {}
    monkeypatch.setattr(mc, "extract_json",
                        lambda prompt, **k: seen.update(prompt=prompt) or reply)
    mc._extract_from_conversation(turns, focus_ts=focus_ts)
    return seen["prompt"]


def test_the_live_trigger_is_marked_and_judged_on_its_own(monkeypatch):
    prompt = _prompt_for(monkeypatch, CHANNEL, focus_ts="4")
    assert ">>> <@U1>: we decided design reviews are on Thursdays" in prompt
    convo = prompt.split("Conversation:\n", 1)[1]
    assert sum(line.startswith(">>> ") for line in convo.splitlines()) == 1
    assert "one being judged" in prompt
    assert "do not make the marked message unresolved" in prompt


def test_backfill_without_a_trigger_keeps_the_whole_thread_prompt(monkeypatch):
    """In a past thread the decision is often not the last message."""
    prompt = _prompt_for(monkeypatch, CHANNEL)
    assert ">>> " not in prompt and "one being judged" not in prompt


def test_a_trigger_missing_from_the_window_falls_back_to_the_plain_prompt(monkeypatch):
    prompt = _prompt_for(monkeypatch, CHANNEL, focus_ts="999")
    assert ">>> " not in prompt and "one being judged" not in prompt


def test_the_trigger_reaches_the_extractor(monkeypatch):
    seen = {}
    monkeypatch.setattr(mc, "_extract_from_conversation",
                        lambda turns, focus_ts=None: seen.update(f=focus_ts) or "m")
    monkeypatch.setattr(mc, "_save", lambda c, m: None)
    mc.capture_conversation(CHANNEL, channel="C1", focus_ts="4")
    assert seen["f"] == "4"


def test_a_rejection_is_logged_not_silent(monkeypatch, caplog):
    """A dropped decision and a hung call looked identical in the terminal."""
    caplog.set_level("INFO", logger="cairn.memcapture")
    _prompt_for(monkeypatch, CHANNEL, focus_ts="4", reply={"is_memorable": False})
    assert "not a decision" in caplog.text
