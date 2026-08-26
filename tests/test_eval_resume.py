"""Resumable eval runs.

A free-tier quota is measured in tens of requests per day; a 35-case golden set
needs ~40 model calls, so it cannot finish in one sitting. Without resume, the
next day's allowance goes on re-proving yesterday's passes and the set never
completes — which is how a project ends up with an eval harness it never runs.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from cairn import eval as ev


@pytest.fixture()
def db(monkeypatch):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "cairn.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE eval_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_id TEXT,
        passed INTEGER)""")
    conn.commit()
    monkeypatch.setenv("DATABASE_PATH", path)
    return conn


def _seed(conn, run_id, rows):
    conn.executemany("INSERT INTO eval_results (run_id, case_id, passed) VALUES (?,?,?)",
                     [(run_id, cid, p) for cid, p in rows])
    conn.commit()


def test_resume_by_id_skips_only_passes(db):
    _seed(db, "abc123", [("a", 1), ("b", 0), ("c", 1)])
    run_id, done = ev._resume_target("abc123")
    assert run_id == "abc123"
    assert done == {"a", "c"}          # "b" failed, so it is retried


def test_a_failure_is_retried_not_recorded_as_final(db):
    """A failure is the thing you are trying to fix, and one failure is not a
    verdict — the classifier's own non-determinism proved that."""
    _seed(db, "r1", [("x", 0)])
    _, done = ev._resume_target("r1")
    assert "x" not in done


def test_resume_last_picks_the_most_recent_run(db):
    _seed(db, "older", [("a", 1)])
    _seed(db, "newer", [("b", 1)])
    run_id, done = ev._resume_target("last")
    assert run_id == "newer" and done == {"b"}


def test_resume_with_no_history_starts_fresh(db):
    run_id, done = ev._resume_target("last")
    assert done == set() and len(run_id) == 12


def test_an_unreadable_db_starts_fresh(monkeypatch):
    """Never block a run because the history could not be read."""
    monkeypatch.setenv("DATABASE_PATH", "/proc/nope/x.db")
    run_id, done = ev._resume_target("last")
    assert done == set() and len(run_id) == 12


def test_unknown_run_id_yields_nothing_to_skip(db):
    _seed(db, "abc123", [("a", 1)])
    run_id, done = ev._resume_target("does-not-exist")
    assert run_id == "does-not-exist" and done == set()
