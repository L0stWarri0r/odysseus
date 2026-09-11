"""Owner-scope email summary / AI-reply / calendar-extraction caches.

Mailing-list Message-IDs are global. Before this change those derived
tables were PK'd on message_id alone, so one user's summary replaced
another's and POST /api/tasks/{id}/clear-cache wiped every tenant.
"""

import sqlite3
from pathlib import Path


def _init_isolated_scheduled_db(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()
    return email_helpers, db_path


def _pk_columns(conn, table: str) -> list[str]:
    info = list(conn.execute(f"PRAGMA table_info({table})"))
    return [row[1] for _pk, row in sorted((row[5], row) for row in info if row[5] > 0)]


def _seed_cache_row(conn, table: str, message_id: str, owner: str, payload: str):
    now = "2026-09-11T00:00:00"
    if table == "email_summaries":
        conn.execute(
            "INSERT OR REPLACE INTO email_summaries "
            "(message_id, owner, uid, folder, subject, sender, summary, model_used, created_at) "
            "VALUES (?, ?, '1', 'INBOX', 'subj', 'a@b', ?, 'm', ?)",
            (message_id, owner, payload, now),
        )
    elif table == "email_ai_replies":
        conn.execute(
            "INSERT OR REPLACE INTO email_ai_replies "
            "(message_id, owner, uid, folder, reply, model_used, created_at) "
            "VALUES (?, ?, '1', 'INBOX', ?, 'm', ?)",
            (message_id, owner, payload, now),
        )
    elif table == "email_calendar_extractions":
        conn.execute(
            "INSERT OR REPLACE INTO email_calendar_extractions "
            "(message_id, owner, uid, events_created, created_at) "
            "VALUES (?, ?, '1', ?, ?)",
            (message_id, owner, int(payload), now),
        )
    else:
        raise AssertionError(table)


def test_fresh_cache_tables_use_message_id_owner_pk(tmp_path, monkeypatch):
    _eh, db_path = _init_isolated_scheduled_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    try:
        for table in (
            "email_summaries",
            "email_ai_replies",
            "email_calendar_extractions",
        ):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            assert "owner" in cols, table
            assert _pk_columns(conn, table) == ["message_id", "owner"], table
    finally:
        conn.close()


def test_legacy_summary_table_migrates_owner_pk_without_dropping_rows(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE email_summaries (
            message_id TEXT PRIMARY KEY,
            uid TEXT, folder TEXT, subject TEXT, sender TEXT,
            summary TEXT NOT NULL, model_used TEXT, created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO email_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("<mid@list.example>", "9", "INBOX", "Hello", "news@list.example",
         "alice leftover", "m", "2020-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        assert "owner" in [r[1] for r in conn.execute("PRAGMA table_info(email_summaries)")]
        assert _pk_columns(conn, "email_summaries") == ["message_id", "owner"]
        row = conn.execute(
            "SELECT message_id, owner, summary FROM email_summaries"
        ).fetchone()
        assert row == ("<mid@list.example>", "", "alice leftover")
    finally:
        conn.close()


def test_same_message_id_two_owners_do_not_clobber(tmp_path, monkeypatch):
    _eh, db_path = _init_isolated_scheduled_db(tmp_path, monkeypatch)
    mid = "<newsletter@example.com>"
    conn = sqlite3.connect(db_path)
    try:
        _seed_cache_row(conn, "email_summaries", mid, "alice", "alice summary")
        _seed_cache_row(conn, "email_summaries", mid, "bob", "bob summary")
        conn.commit()
        rows = {
            owner: summary
            for owner, summary in conn.execute(
                "SELECT owner, summary FROM email_summaries WHERE message_id = ?",
                (mid,),
            )
        }
        assert rows == {"alice": "alice summary", "bob": "bob summary"}
    finally:
        conn.close()


def test_cache_read_clause_hides_other_owners_when_authenticated():
    import routes.email_helpers as email_helpers

    clause, params = email_helpers._email_cache_owner_clause("alice")
    assert clause == "owner = ?"
    assert params == ["alice"]
    assert "owner = ''" not in clause
    assert "OR" not in clause

    clause, params = email_helpers._email_cache_owner_clause("")
    assert "owner = ''" in clause
    assert "owner IS NULL" in clause
    assert params == []


def test_existing_ids_are_owner_scoped(tmp_path, monkeypatch):
    email_helpers, db_path = _init_isolated_scheduled_db(tmp_path, monkeypatch)
    mid = "<shared@example.com>"
    conn = sqlite3.connect(db_path)
    try:
        _seed_cache_row(conn, "email_summaries", mid, "alice", "a")
        _seed_cache_row(conn, "email_summaries", mid, "bob", "b")
        _seed_cache_row(conn, "email_ai_replies", mid, "bob", "reply")
        _seed_cache_row(conn, "email_calendar_extractions", mid, "alice", "1")
        conn.commit()

        assert email_helpers._email_cache_existing_ids(conn, "email_summaries", "alice") == {mid}
        assert email_helpers._email_cache_existing_ids(conn, "email_summaries", "bob") == {mid}
        assert email_helpers._email_cache_existing_ids(conn, "email_ai_replies", "alice") == set()
        assert email_helpers._email_cache_existing_ids(conn, "email_ai_replies", "bob") == {mid}
        assert email_helpers._email_cache_existing_ids(conn, "email_calendar_extractions", "bob") == set()
        assert email_helpers._email_cache_existing_ids(conn, "email_calendar_extractions", "alice") == {mid}
    finally:
        conn.close()


def test_clear_cache_does_not_wipe_other_owner_rows(tmp_path, monkeypatch):
    email_helpers, db_path = _init_isolated_scheduled_db(tmp_path, monkeypatch)
    mid = "<shared@example.com>"
    conn = sqlite3.connect(db_path)
    try:
        for table, alice_payload, bob_payload, legacy_payload in (
            ("email_summaries", "alice s", "bob s", "legacy s"),
            ("email_ai_replies", "alice r", "bob r", "legacy r"),
            ("email_calendar_extractions", "1", "2", "3"),
        ):
            _seed_cache_row(conn, table, mid, "alice", alice_payload)
            _seed_cache_row(conn, table, mid, "bob", bob_payload)
            _seed_cache_row(conn, table, mid + "-legacy", "", legacy_payload)
        conn.commit()
    finally:
        conn.close()

    cleared = email_helpers.clear_email_cache_tables(
        ("email_summaries", "email_ai_replies", "email_calendar_extractions"),
        owner="alice",
        db_path=db_path,
    )
    assert cleared["email_summaries"] == 2  # alice + legacy
    assert cleared["email_ai_replies"] == 2
    assert cleared["email_calendar_extractions"] == 2

    conn = sqlite3.connect(db_path)
    try:
        for table in (
            "email_summaries",
            "email_ai_replies",
            "email_calendar_extractions",
        ):
            owners = {
                r[0]
                for r in conn.execute(f"SELECT owner FROM {table}")
            }
            assert owners == {"bob"}, table
    finally:
        conn.close()


def test_clear_cache_unauthenticated_still_clears_all_rows(tmp_path, monkeypatch):
    email_helpers, db_path = _init_isolated_scheduled_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    try:
        _seed_cache_row(conn, "email_summaries", "<a>", "alice", "a")
        _seed_cache_row(conn, "email_summaries", "<b>", "bob", "b")
        conn.commit()
    finally:
        conn.close()

    cleared = email_helpers.clear_email_cache_tables(
        ("email_summaries",),
        owner="",
        db_path=db_path,
    )
    assert cleared["email_summaries"] == 2
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM email_summaries").fetchone()[0] == 0
    finally:
        conn.close()


def test_task_clear_cache_delegates_to_owner_scoped_helper():
    src = Path("routes/task_routes.py").read_text(encoding="utf-8")
    assert "clear_email_cache_tables" in src
    assert "DELETE FROM email_summaries" not in src
    assert "DELETE FROM email_ai_replies" not in src
    assert "DELETE FROM email_calendar_extractions" not in src


def test_poller_and_routes_stamp_owner_on_cache_writes():
    poller = Path("routes/email_pollers.py").read_text(encoding="utf-8")
    routes = Path("routes/email_routes.py").read_text(encoding="utf-8")
    assert "INSERT OR REPLACE INTO email_summaries" in poller
    assert "(message_id, owner, uid, folder, subject, sender, summary, model_used, created_at)" in poller
    assert "(message_id, owner, uid, folder, reply, model_used, created_at)" in poller
    assert "(message_id, owner, uid, events_created, created_at)" in poller
    assert "_email_cache_existing_ids" in poller
    assert "(message_id, owner, uid, folder, subject, sender, summary, model_used, created_at)" in routes
    assert "(message_id, owner, uid, folder, reply, model_used, created_at)" in routes
    assert "_email_cache_owner_clause" in routes
