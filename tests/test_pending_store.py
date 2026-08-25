"""Staged Jira tickets survive a restart.

`_pending` used to be a process dict: a ticket waits on a human typing "yes",
which can be hours, and any restart in between silently dropped it — the user's
"yes" then did nothing, with no error to explain why.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from loop import jira_tool


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("DATABASE_PATH", os.path.join(d, "t.db"))
        yield


def _store():
    return jira_tool._PendingStore()


TICKET = {"summary": "checkout 500s", "description": "on submit", "priority": "High"}


def test_round_trips_a_ticket():
    s = _store()
    s["1.1"] = TICKET
    assert s.get("1.1") == TICKET
    assert "1.1" in s


def test_survives_a_restart():
    _store()["1.1"] = TICKET               # written by one "process"
    assert _store().get("1.1") == TICKET   # read by a fresh one — the whole point


def test_pop_returns_then_clears():
    s = _store()
    s["1.1"] = TICKET
    assert s.pop("1.1") == TICKET
    assert s.pop("1.1") is None
    assert "1.1" not in s


def test_missing_thread_is_absent():
    s = _store()
    assert s.get("nope") is None and "nope" not in s
    assert s.pop("nope", "fallback") == "fallback"


def test_restaging_replaces_not_duplicates():
    s = _store()
    s["1.1"] = TICKET
    s["1.1"] = {**TICKET, "priority": "Low"}
    assert s.get("1.1")["priority"] == "Low"


def test_threads_are_independent():
    s = _store()
    s["1.1"] = TICKET
    s["2.2"] = {**TICKET, "summary": "other"}
    assert s.pop("1.1")["summary"] == "checkout 500s"
    assert s.get("2.2")["summary"] == "other"


def test_stale_tickets_expire(monkeypatch):
    s = _store()
    s["1.1"] = TICKET
    # capture the real clock BEFORE patching, or the lambda recurses into itself
    future = jira_tool.time.time() + jira_tool._PENDING_TTL_S + 60
    monkeypatch.setattr(jira_tool.time, "time", lambda: future)
    assert s.get("1.1") is None            # an ancient "yes" is not a confirmation


def test_unwritable_db_degrades_quietly(monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", "/proc/definitely/not/writable/x.db")
    s = _store()
    s["1.1"] = TICKET                      # must not raise
    assert s.get("1.1") is None
