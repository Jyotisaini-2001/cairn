"""Tool toggles (CAIRN_TOOLS).

Every tool's schema is re-sent on each request, so tools are switches rather than
permanent fixtures. These cover the switch itself, not agent behaviour (that is
the golden set's job) — a single agent turn never exercises the env parsing.
"""
from __future__ import annotations

import os

from loop.agent import _DEFAULT_TOOLS, _TOOL_GROUPS, _enabled_tools


def _with(value: str | None):
    if value is None:
        os.environ.pop("CAIRN_TOOLS", None)
    else:
        os.environ["CAIRN_TOOLS"] = value
    return _enabled_tools()


def _names(tools) -> set[str]:
    """Short name per tool. Some strands_tools entries are *modules*, whose
    __name__ is the dotted path ("strands_tools.calculator"), so take the last
    segment; @tool-decorated functions expose tool_name directly."""
    out = set()
    for t in tools:
        name = getattr(t, "tool_name", None) or getattr(t, "__name__", str(t))
        out.add(name.rsplit(".", 1)[-1])
    return out


def test_default_matches_documented_default():
    tools = _with(None)
    assert len(tools) == len(_with(_DEFAULT_TOOLS))
    assert "calculator" in _names(tools)


def test_think_is_off_by_default_and_can_be_enabled():
    assert "think" not in _names(_with(None))
    assert "think" in _names(_with("slack,jira,calculator,think"))


def test_single_group_selects_only_that_group():
    assert len(_with("slack")) == 2          # slack + slack_send_message
    assert len(_with("calculator")) == 1
    assert len(_with("jira")) == 3


def test_none_disables_every_optional_tool():
    for value in ("none", "off", ""):
        assert _with(value) == []


def test_unknown_group_is_skipped_not_fatal():
    tools = _with("slack,definitely-not-a-tool")
    assert len(tools) == 2                    # typo degrades, never raises


def test_whitespace_and_case_are_tolerated():
    assert len(_with("  SLACK , Jira  ")) == 5


def test_every_group_builds():
    for name in _TOOL_GROUPS:
        assert _with(name), f"{name} produced no tools"


def teardown_module(_m):
    os.environ.pop("CAIRN_TOOLS", None)
