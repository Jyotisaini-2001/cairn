"""Tests for Jira project-key + assignee resolution (no follow-ups).

Runnable two ways:
    python -m tests.test_jira
    pytest tests/test_jira.py

No network — Jira project list and the Slack member list are stubbed.
"""
from __future__ import annotations

import os


def _stub_projects(monkey: list[tuple[str, str]]):
    import loop.jira_tool as jt

    jt._list_projects = lambda: monkey  # type: ignore[assignment]


def _stub_users(members: list[dict]):
    import loop.jira_tool as jt

    jt._slack_users = lambda: members  # type: ignore[assignment]


def test_resolve_project_key_matches_key_and_name() -> None:
    import loop.jira_tool as jt

    os.environ["JIRA_PROJECT_KEY"] = "KAN"
    _stub_projects([("KAN", "Kanban"), ("BAN", "Banana Board")])
    assert jt.resolve_project_key("BAN") == ("BAN", None)          # exact key
    assert jt.resolve_project_key("ban") == ("BAN", None)          # case-insensitive
    assert jt.resolve_project_key("Banana Board") == ("BAN", None)  # exact name
    assert jt.resolve_project_key("banana") == ("BAN", None)        # name substring


def test_resolve_project_key_falls_back_to_default_with_note() -> None:
    import loop.jira_tool as jt

    os.environ["JIRA_PROJECT_KEY"] = "KAN"
    _stub_projects([("KAN", "Kanban")])
    key, note = jt.resolve_project_key("NOPE")
    assert key == "KAN", key
    assert note and "not found" in note and "KAN" in note, note


def test_resolve_project_key_trusts_input_when_list_unavailable() -> None:
    import loop.jira_tool as jt

    _stub_projects([])  # e.g. API failure — must not block filing
    assert jt.resolve_project_key("ZZZ") == ("ZZZ", None)


def test_resolve_project_key_empty_uses_default() -> None:
    import loop.jira_tool as jt

    os.environ["JIRA_PROJECT_KEY"] = "KAN"
    _stub_projects([("KAN", "Kanban")])
    assert jt.resolve_project_key("") == ("KAN", None)


_MEMBERS = [
    {"name": "jsaini", "real_name": "Jyoti Saini", "profile": {"email": "js@x.com", "real_name": "Jyoti Saini"}},
    {"name": "jverma", "real_name": "Jyoti Verma", "profile": {"email": "jv@x.com", "real_name": "Jyoti Verma"}},
    {"name": "john", "real_name": "John Doe", "profile": {"email": "john@x.com", "display_name": "john"}},
    {"deleted": True, "profile": {"email": "gone@x.com", "real_name": "Ghost"}},
    {"is_bot": True, "profile": {"email": "bot@x.com", "real_name": "Loop Bot"}},
]


def test_email_from_full_name_unique() -> None:
    import loop.jira_tool as jt

    _stub_users(_MEMBERS)
    assert jt._slack_email_from_name("Jyoti Saini") == "js@x.com"
    assert jt._slack_email_from_name("John Doe") == "john@x.com"


def test_email_from_first_name_unique() -> None:
    import loop.jira_tool as jt

    _stub_users(_MEMBERS)
    assert jt._slack_email_from_name("John") == "john@x.com"
    assert jt._slack_email_from_name("@john") == "john@x.com"  # leading @ tolerated


def test_email_ambiguous_first_name_returns_none() -> None:
    import loop.jira_tool as jt

    _stub_users(_MEMBERS)
    # Two people whose first name is "Jyoti" and no exact username/display match.
    assert jt._slack_email_from_name("Jyoti") is None


def test_email_excludes_bots_and_deleted() -> None:
    import loop.jira_tool as jt

    _stub_users(_MEMBERS)
    assert jt._slack_email_from_name("Ghost") is None
    assert jt._slack_email_from_name("Loop Bot") is None
    assert jt._slack_email_from_name("Nobody") is None


def test_resolve_assignee_email_name_first() -> None:
    import loop.jira_tool as jt

    _stub_users(_MEMBERS)
    # A resolvable NAME wins over a passed email — the model often echoes the
    # requester's identity, which must not override the named assignee.
    assert jt._resolve_assignee_email(assignee_email="req@x.com", assignee_name="Jyoti Saini") == "js@x.com"
    assert jt._resolve_assignee_email(assignee_name="Jyoti Saini") == "js@x.com"
    # A name that doesn't resolve falls back to the explicit email.
    assert jt._resolve_assignee_email(assignee_name="Nobody", assignee_email="req@x.com") == "req@x.com"
    assert jt._resolve_assignee_email(assignee_email="req@x.com") == "req@x.com"
    assert jt._resolve_assignee_email(assignee_name="Nobody") == ""


def test_name_beats_requester_slack_mention() -> None:
    """Reproduces the live bug: the agent passed assignee_name='Jyoti' AND the
    requester's slack_mention; the ticket must go to Jyoti, not the requester."""
    import loop.jira_tool as jt

    _stub_users(_MEMBERS)
    jt._slack_email_from_user_id = lambda uid: "requester@x.com"  # the spurious requester
    got = jt._resolve_assignee_email(assignee_name="Jyoti Saini", slack_mention="<@UREQUESTER>")
    assert got == "js@x.com", got  # NOT requester@x.com


def test_assignee_notes() -> None:
    import loop.jira_tool as jt

    inline, flag = jt._assignee_notes({"assignee": {"accountId": "1"}}, "js@x.com", "Jyoti Saini")
    assert inline == ", assigned to js@x.com" and flag == ""
    inline, flag = jt._assignee_notes({}, "", "Jyoti")
    assert inline == "" and "left unassigned" in flag and "Jyoti" in flag
    assert jt._assignee_notes({}, "", "") == ("", "")


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
