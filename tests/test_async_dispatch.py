"""Off-thread dispatch (CAIRN_ASYNC).

Socket Mode wants an ack within ~3s; an agent turn takes seconds and may retry a
rate-limited provider on top. Running inline held the socket past the deadline,
so Slack re-delivered the event and the user got the same answer twice.
"""
from __future__ import annotations

import os
import threading

from cairn import slack_app


def setup_function(_f):
    os.environ.pop("CAIRN_ASYNC", None)
    os.environ.pop("CAIRN_ASYNC_WORKERS", None)
    slack_app._executor = None


def test_work_runs_and_receives_its_arguments():
    got, done = {}, threading.Event()

    def fn(a, b=None):
        got.update(a=a, b=b)
        done.set()

    slack_app._dispatch(fn, 1, b=2)
    assert done.wait(5)
    assert got == {"a": 1, "b": 2}


def test_dispatch_returns_before_the_work_finishes():
    """The point of the change: the socket thread must not wait."""
    release, started = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)

    slack_app._dispatch(slow)
    assert started.wait(5)          # running...
    assert not release.is_set()     # ...and _dispatch already returned
    release.set()


def test_worker_exceptions_do_not_escape():
    done = threading.Event()

    def boom():
        done.set()
        raise RuntimeError("kaboom")

    slack_app._dispatch(boom)       # must not raise into the caller
    assert done.wait(5)


def test_off_runs_inline_on_the_calling_thread():
    os.environ["CAIRN_ASYNC"] = "off"
    seen = {}
    slack_app._dispatch(lambda: seen.update(t=threading.current_thread().name))
    assert seen["t"] == threading.current_thread().name


def test_off_lets_exceptions_surface_for_debugging():
    os.environ["CAIRN_ASYNC"] = "off"
    try:
        slack_app._dispatch(lambda: 1 / 0)
    except ZeroDivisionError:
        return
    raise AssertionError("inline mode should not swallow errors")


def test_worker_count_is_configurable():
    os.environ["CAIRN_ASYNC_WORKERS"] = "2"
    assert slack_app._pool()._max_workers == 2


def test_bad_worker_count_falls_back():
    os.environ["CAIRN_ASYNC_WORKERS"] = "abc"
    assert slack_app._pool()._max_workers == 4


def teardown_module(_m):
    setup_function(None)
