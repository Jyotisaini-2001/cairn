"""Stripping credentials before anything is written down.

Memories are stored word for word, and a decision is exactly the kind of message
that carries a credential — "ok let's go with the new provider, key is sk-live-…".
Without this, the key lands in the database, gets embedded into a vector, and is
read back months later by anyone who asks the right question.
"""
from __future__ import annotations

import pytest

from cairn.redact import redact


def _clean(text):
    return redact(text)[0]


def _kinds(text):
    return redact(text)[1]


# ------------------------------------------------------------------ secrets
@pytest.mark.parametrize("secret,kind", [
    ("sk-live-a8f3k2b9c1d4e7f0", "api-key"),
    ("xoxb-1234567890-abcdefghijkl", "slack-token"),
    ("ghp_16CharsMinimumAAAA", "github-token"),
    ("AKIAIOSFODNN7EXAMPLE", "aws-key"),
    ("AIzaSyD-1234567890abcdefghijklmnopqrstu", "google-key"),
])
def test_provider_keys_never_reach_the_store(secret, kind):
    out, kinds = redact(f"we're switching providers, key is {secret}")
    assert secret not in out
    assert kind in kinds
    assert "switching providers" in out          # the decision survives


def test_a_password_in_a_connection_string_is_stripped():
    out = _clean("db url is postgres://admin:hunter2@db.internal:5432/billing")
    assert "hunter2" not in out
    assert "db.internal" in out                  # the useful half is kept


def test_a_self_naming_secret_keeps_its_label():
    """"password: [redacted]" still tells the reader what was there."""
    out = _clean("password: sup3rs3cret!!")
    assert "sup3rs3cret" not in out
    assert "password" in out.lower()


def test_a_private_key_block_goes_entirely():
    text = ("rotate this one:\n-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxyz\nabc\n-----END RSA PRIVATE KEY-----")
    out = _clean(text)
    assert "MIIEowIBAAKCAQEAxyz" not in out
    assert "rotate this one" in out


def test_emails_and_card_numbers_are_caught():
    assert "priya.sharma@company.com" not in _clean("invoices to priya.sharma@company.com")
    assert "4111 1111 1111 1111" not in _clean("card 4111 1111 1111 1111 on file")


# ------------------------------------------------------- what must be kept
def test_an_ordinary_decision_is_untouched():
    text = "we chose Postgres for billing because Mongo's transactions were flaky"
    assert _clean(text) == text
    assert _kinds(text) == []


def test_slack_references_survive():
    """<@U…> and <#C…> look like long tokens but they are the citation — losing
    them would break the thing a memory exists for."""
    text = "we decided <@U0BS8SSEF8V> owns deploys in <#C0BRXCZBBP1>"
    assert _clean(text) == text


def test_version_numbers_and_dates_are_not_card_numbers():
    for text in ("we're on v2.5.1 now", "the deadline is 2026-08-26", "cap it at 24 hours"):
        assert _clean(text) == text


# ------------------------------------------------------------------ config
def test_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("CAIRN_REDACT", "off")
    text = "key is sk-live-a8f3k2b9c1d4e7f0"
    assert _clean(text) == text


def test_categories_can_be_narrowed(monkeypatch):
    """Some teams genuinely want an email kept — "invoices go to billing@…"."""
    monkeypatch.setenv("CAIRN_REDACT_KINDS", "api-key")
    out = _clean("key sk-live-a8f3k2b9c1d4e7f0 and mail ops@company.com")
    assert "sk-live" not in out
    assert "ops@company.com" in out


def test_empty_input_is_safe():
    assert redact("") == ("", [])
    assert redact(None) == (None, [])


# ------------------------------------------------- applied at the store layer
def test_the_store_redacts_on_write(monkeypatch, tmp_path):
    """Redaction lives at the store because that is the one point every write
    passes through — add_memory, the direct remember() path, passive capture and
    backfill. Anywhere else and one of them stays uncovered."""
    import asyncio

    monkeypatch.setenv("CAIRN_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "d.db"))
    from cairn.storage import SqliteMemoryStore

    store = SqliteMemoryStore()
    asyncio.run(store.add("switching providers, key is sk-live-a8f3k2b9c1d4e7f0"))
    rows = store._conn.execute("SELECT content FROM memory_entries").fetchall()
    assert rows and "sk-live" not in rows[0][0]
    assert "switching providers" in rows[0][0]
