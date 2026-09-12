"""Owner isolation for email_boundaries, sender_signatures, and urgency alerts."""

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _pragma_pk(conn, table: str) -> list[str]:
    info = list(conn.execute(f"PRAGMA table_info({table})"))
    return [row[1] for row in sorted(info, key=lambda r: r[5] or 999) if row[5]]


def _seed_scheduled_db(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()
    return email_helpers, db_path


def test_derivative_owner_clause_fail_closed_for_authenticated_owner():
    from routes.email_helpers import _email_derivative_owner_clause

    clause, params = _email_derivative_owner_clause("alice")
    assert clause == "owner = ?"
    assert params == ["alice"]
    assert "owner IS NULL" not in clause


def test_derivative_owner_clause_keeps_legacy_rows_for_single_user():
    from routes.email_helpers import _email_derivative_owner_clause

    clause, params = _email_derivative_owner_clause("")
    assert clause == "(owner = '' OR owner IS NULL)"
    assert params == []


def test_new_cache_tables_use_owner_composite_pk(tmp_path, monkeypatch):
    _helpers, db_path = _seed_scheduled_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    try:
        assert _pragma_pk(conn, "email_boundaries") == ["message_id", "owner"]
        assert _pragma_pk(conn, "sender_signatures") == ["from_address", "owner"]
        assert _pragma_pk(conn, "email_urgency_alerts") == ["message_id", "owner"]
        cols = {row[1] for row in conn.execute("PRAGMA table_info(email_boundaries)")}
        assert "turns_json" in cols
    finally:
        conn.close()


def test_legacy_cache_tables_migrate_to_owner_pk(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE email_boundaries (
            message_id TEXT PRIMARY KEY,
            uid TEXT,
            folder TEXT,
            sig_start INTEGER,
            quote_start INTEGER,
            model_used TEXT,
            created_at TEXT NOT NULL,
            turns_json TEXT
        );
        CREATE TABLE sender_signatures (
            from_address TEXT PRIMARY KEY,
            signature_text TEXT,
            sample_count INTEGER,
            last_built_at TEXT NOT NULL,
            model_used TEXT,
            source TEXT
        );
        CREATE TABLE email_urgency_alerts (
            message_id TEXT PRIMARY KEY,
            uid TEXT,
            folder TEXT,
            subject TEXT,
            sender TEXT,
            urgency TEXT,
            reason TEXT,
            alerted INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO email_boundaries "
        "(message_id, uid, folder, sig_start, quote_start, model_used, created_at, turns_json) "
        "VALUES (?, '1', 'INBOX', 10, 20, 'm', ?, NULL)",
        ("<mid@list>", now),
    )
    conn.execute(
        "INSERT INTO sender_signatures "
        "(from_address, signature_text, sample_count, last_built_at, model_used, source) "
        "VALUES ('news@list.example', 'Best', 3, ?, 'm', 'llm')",
        (now,),
    )
    conn.execute(
        "INSERT INTO email_urgency_alerts "
        "(message_id, uid, folder, subject, sender, urgency, reason, alerted, created_at) "
        "VALUES ('<mid@list>', '1', 'INBOX', 'Hi', 'news@list.example', 'high', 'soon', 1, ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        assert _pragma_pk(conn, "email_boundaries") == ["message_id", "owner"]
        assert _pragma_pk(conn, "sender_signatures") == ["from_address", "owner"]
        assert _pragma_pk(conn, "email_urgency_alerts") == ["message_id", "owner"]
        assert conn.execute(
            "SELECT owner, sig_start FROM email_boundaries WHERE message_id = ?",
            ("<mid@list>",),
        ).fetchone() == ("", 10)
        assert conn.execute(
            "SELECT owner, signature_text FROM sender_signatures WHERE from_address = ?",
            ("news@list.example",),
        ).fetchone() == ("", "Best")
        assert conn.execute(
            "SELECT owner, urgency FROM email_urgency_alerts WHERE message_id = ?",
            ("<mid@list>",),
        ).fetchone() == ("", "high")
    finally:
        conn.close()


def test_two_owners_can_cache_the_same_message_and_sender(tmp_path, monkeypatch):
    helpers, db_path = _seed_scheduled_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO email_boundaries "
            "(message_id, owner, uid, folder, sig_start, quote_start, model_used, created_at) "
            "VALUES ('<shared@list>', 'alice', '11', 'INBOX', 4, -1, 'm', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO email_boundaries "
            "(message_id, owner, uid, folder, sig_start, quote_start, model_used, created_at) "
            "VALUES ('<shared@list>', 'bob', '22', 'INBOX', 9, -1, 'm', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO sender_signatures "
            "(from_address, owner, signature_text, sample_count, last_built_at, model_used, source) "
            "VALUES ('news@list.example', 'alice', 'Alice-fold', 3, ?, 'm', 'llm')",
            (now,),
        )
        conn.execute(
            "INSERT INTO sender_signatures "
            "(from_address, owner, signature_text, sample_count, last_built_at, model_used, source) "
            "VALUES ('news@list.example', 'bob', 'Bob-fold', 3, ?, 'm', 'llm')",
            (now,),
        )
        conn.execute(
            "INSERT INTO email_urgency_alerts "
            "(message_id, owner, uid, folder, subject, sender, urgency, reason, alerted, created_at) "
            "VALUES ('<shared@list>', 'alice', '11', 'INBOX', 'Hi', 'news@list.example', 'high', 'a', 1, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO email_urgency_alerts "
            "(message_id, owner, uid, folder, subject, sender, urgency, reason, alerted, created_at) "
            "VALUES ('<shared@list>', 'bob', '22', 'INBOX', 'Hi', 'news@list.example', 'none', 'b', 0, ?)",
            (now,),
        )
        conn.commit()

        assert helpers.lookup_cached_email_boundaries(conn, "<shared@list>", "alice")[0] == 4
        assert helpers.lookup_cached_email_boundaries(conn, "<shared@list>", "bob")[0] == 9
        assert helpers.lookup_cached_sender_signature(conn, "news@list.example", "alice")[0] == "Alice-fold"
        assert helpers.lookup_cached_sender_signature(conn, "news@list.example", "bob")[0] == "Bob-fold"

        alice_ids = helpers._email_derivative_existing_ids(
            conn, "email_boundaries", "message_id", "alice",
        )
        bob_ids = helpers._email_derivative_existing_ids(
            conn, "email_urgency_alerts", "message_id", "bob",
        )
        assert alice_ids == {"<shared@list>"}
        assert helpers._email_derivative_existing_ids(
            conn, "email_boundaries", "message_id", "carol",
        ) == set()
        assert bob_ids == {"<shared@list>"}
    finally:
        conn.close()


def test_authenticated_lookup_does_not_see_other_owner_or_legacy_rows(tmp_path, monkeypatch):
    helpers, db_path = _seed_scheduled_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO email_boundaries "
            "(message_id, owner, uid, folder, sig_start, quote_start, model_used, created_at) "
            "VALUES ('<legacy@list>', '', '1', 'INBOX', 1, -1, 'm', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO email_boundaries "
            "(message_id, owner, uid, folder, sig_start, quote_start, model_used, created_at) "
            "VALUES ('<legacy@list>', 'bob', '2', 'INBOX', 2, -1, 'm', ?)",
            (now,),
        )
        conn.commit()
        assert helpers.lookup_cached_email_boundaries(conn, "<legacy@list>", "alice") is None
        assert helpers.lookup_cached_email_boundaries(conn, "<legacy@list>", "")[0] == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_mark_boundaries_forwards_owner_to_imap(monkeypatch):
    calls = []

    def fake_imap(account_id=None, owner=""):
        calls.append((account_id, owner))
        raise RuntimeError("stop-imap")

    monkeypatch.setattr("routes.email_helpers._imap_connect", fake_imap)
    from src.builtin_actions import action_mark_email_boundaries

    msg, ok = await action_mark_email_boundaries(owner="alice")
    assert ok is False
    assert "stop-imap" in str(msg)
    assert calls == [(None, "alice")]


@pytest.mark.asyncio
async def test_learn_signatures_forwards_owner_to_imap(monkeypatch):
    calls = []

    def fake_imap(account_id=None, owner=""):
        calls.append((account_id, owner))
        raise RuntimeError("stop-imap")

    monkeypatch.setattr("routes.email_helpers._imap_connect", fake_imap)
    from src.builtin_actions import action_learn_sender_signatures

    msg, ok = await action_learn_sender_signatures(owner="bob")
    assert ok is False
    assert "stop-imap" in str(msg)
    assert calls == [(None, "bob")]


@pytest.mark.asyncio
async def test_clear_cache_leaves_other_owners_boundary_rows(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.task_routes as task_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO email_boundaries "
        "(message_id, owner, uid, folder, sig_start, quote_start, model_used, created_at) "
        "VALUES ('<a@x>', 'alice', '1', 'INBOX', 1, -1, 'm', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO email_boundaries "
        "(message_id, owner, uid, folder, sig_start, quote_start, model_used, created_at) "
        "VALUES ('<b@x>', 'bob', '2', 'INBOX', 2, -1, 'm', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO email_boundaries "
        "(message_id, owner, uid, folder, sig_start, quote_start, model_used, created_at) "
        "VALUES ('<legacy@x>', '', '3', 'INBOX', 3, -1, 'm', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    class FakeTask:
        id = "task-1"
        owner = "alice"
        action = "mark_email_boundaries"

    class FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return FakeTask()

    class FakeDB:
        def query(self, *args, **kwargs):
            return FakeQuery()

        def close(self):
            pass

    monkeypatch.setattr(task_routes, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(task_routes, "get_current_user", lambda request: "alice")

    router = task_routes.setup_task_routes(MagicMock())
    try:
        clear_cache = _route_endpoint(router, "/{task_id}/clear-cache", "POST")
    except AssertionError:
        clear_cache = _route_endpoint(router, "/api/tasks/{task_id}/clear-cache", "POST")
    result = await clear_cache(MagicMock(), "task-1")

    assert result["ok"] is True
    assert result["cleared"]["email_boundaries"] == 2

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT owner FROM email_boundaries ORDER BY owner"
        ).fetchall()
        assert rows == [("bob",)]
    finally:
        conn.close()


def test_urgency_cache_files_only_delete_owner_accounts(tmp_path, monkeypatch):
    from routes import email_helpers

    cache_dir = tmp_path / "email_urgency_cache"
    cache_dir.mkdir()
    (cache_dir / "acct-alice.json").write_text("{}", encoding="utf-8")
    (cache_dir / "acct-bob.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(email_helpers, "_urgency_cache_account_ids", lambda owner: {"acct-alice"})

    removed = email_helpers.clear_email_urgency_cache_files(
        owner="alice", cache_dir=cache_dir,
    )
    assert removed == 1
    assert (cache_dir / "acct-bob.json").exists()
    assert not (cache_dir / "acct-alice.json").exists()


def test_urgency_cache_files_wipe_all_in_single_user_mode(tmp_path):
    from routes import email_helpers

    cache_dir = tmp_path / "email_urgency_cache"
    cache_dir.mkdir()
    (cache_dir / "acct-alice.json").write_text("{}", encoding="utf-8")
    (cache_dir / "acct-bob.json").write_text("{}", encoding="utf-8")

    removed = email_helpers.clear_email_urgency_cache_files(owner="", cache_dir=cache_dir)
    assert removed == 2
    assert list(cache_dir.glob("*.json")) == []
