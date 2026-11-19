"""Tests for telemetry persistence + auto-reconnect on a stale/readonly DB.

Runnable two ways:
    python -m tests.test_observability
    pytest tests/test_observability.py

No network. Uses a throwaway telemetry DB; simulates the "attempt to write a
readonly database" that hits a long-running process whose DB file was swapped.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path


def _fresh_db() -> str:
    import cairn.observability as obs

    path = str(Path(tempfile.mkdtemp()) / "obs.db")
    os.environ["DATABASE_PATH"] = path
    obs._conn = None
    obs._db()
    return path


def test_execute_write_roundtrip() -> None:
    import cairn.observability as obs

    _fresh_db()
    obs._execute_write(
        "INSERT INTO steps (request_id, seq, kind, name) VALUES (?,?,?,?)",
        ("rid1", 1, "tool", "slack"),
    )
    row = obs._db().execute("SELECT request_id, kind, name FROM steps WHERE request_id='rid1'").fetchone()
    assert row == ("rid1", "tool", "slack"), row


class _FailOnceConn(sqlite3.Connection):
    """A real connection that raises readonly ONCE on execute, then behaves — a
    stand-in for a stale/swapped handle (you can't patch execute on the C type)."""

    armed = True

    def execute(self, *a, **k):  # type: ignore[override]
        if _FailOnceConn.armed:
            _FailOnceConn.armed = False
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return super().execute(*a, **k)


def test_execute_write_reconnects_on_operationalerror() -> None:
    import cairn.observability as obs

    path = _fresh_db()  # schema created on a normal conn
    # Swap the cached handle for one rigged to fail its first write.
    _FailOnceConn.armed = True
    obs._conn = sqlite3.connect(path, factory=_FailOnceConn, check_same_thread=False)
    rigged = obs._conn

    # Should hit readonly once, drop+reopen the connection, and land the row.
    obs._execute_write(
        "INSERT INTO steps (request_id, seq, kind, name) VALUES (?,?,?,?)",
        ("rid2", 1, "model", "MiniMax-M3"),
    )
    assert _FailOnceConn.armed is False, "the failure path should have fired once"
    assert obs._conn is not rigged, "connection should have been reopened"
    row = obs._db().execute("SELECT request_id, name FROM steps WHERE request_id='rid2'").fetchone()
    assert row == ("rid2", "MiniMax-M3"), row


def test_record_step_swallows_persistent_failure() -> None:
    import cairn.observability as obs

    _fresh_db()

    class _AlwaysReadonly:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        def close(self):
            pass

    orig_db = obs._db
    obs._db = lambda: _AlwaysReadonly()  # type: ignore[assignment]
    try:
        # Must NOT raise even when every attempt fails — telemetry never breaks a request.
        obs.record_step("rid3", 1, "tool", "think", detail={"x": 1})
    finally:
        obs._db = orig_db  # type: ignore[assignment]


def test_reset_conn_safe_when_none() -> None:
    import cairn.observability as obs

    obs._conn = None
    obs._reset_conn()  # must not raise
    assert obs._conn is None


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
