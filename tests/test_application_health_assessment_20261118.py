"""POST /api/session/{id}/merge-last-assistant must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to merge assistant messages"}.
The exception is logged and is not returned. An HTTPException raised inside
the handler is still passed through. A missing in-memory session stays
HTTP 404 "Session not found". Invalid JSON and a JSON list fail before
session lookup and do not save. Fewer than two in-memory assistants is
{"status": "ok", "merged": false} and does not open the database or save.
Success with a merge stays {"status": "ok", "merged": true}.

The in-memory merge happens before the database query. A query failure
leaves that memory change in place, does not roll back, and does not save
sessions. save_sessions() runs only after the database block, once, on the
merged path and also when save_sessions itself raises. Ownership is still
checked before the try. History read, truncate, add-message, delete-messages,
edit-message, mark-stopped, update-last-meta, and fork were not changed.
The chat client does not read the merge body.
"""
import copy
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.history_routes as history_routes
from core.database import ChatMessage as DbChatMessage, Session as DbSession, SessionLocal
from core.models import ChatMessage

_MERGE_SECRET = "MERGE_SECRET session assistant merge token=do-not-return"
_KEY_SECRET = "KEY_SECRET session missing token=do-not-return"
_JSON_SECRET = "JSON_SECRET session body token=do-not-return"
_QUERY_SECRET = "QUERY_SECRET session query token=do-not-return"
_SAVE_SECRET = "SAVE_SECRET session save token=do-not-return"
_FIXED_ERROR = "Failed to merge assistant messages"
_SECRETS = (_MERGE_SECRET, _KEY_SECRET, _JSON_SECRET, _QUERY_SECRET, _SAVE_SECRET)
_REPO = Path(__file__).resolve().parent.parent
_MODEL = "test-model"
_LIST_GET = "'list' object has no attribute 'get'"
_NULL_CONCAT = 'can only concatenate str (not "NoneType") to str'
_LOG = "Session assistant merge failed"


class _MemSession:
    def __init__(self, history):
        self.history = list(history)


class _Manager:
    def __init__(self, session=None, exc=None, save_exc=None):
        self.session = session if session is not None else _MemSession([])
        self.exc = exc
        self.save_exc = save_exc
        self.calls = []
        self.saves = 0

    def get_session(self, session_id):
        self.calls.append(session_id)
        if self.exc is not None:
            raise self.exc
        return self.session

    def save_sessions(self):
        self.saves += 1
        if self.save_exc is not None:
            raise self.save_exc


class _FailSession:
    def __init__(self, exc):
        self.exc = exc
        self.closed = False
        self.rollbacks = 0

    def query(self, *args, **kwargs):
        raise self.exc

    def close(self):
        self.closed = True

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        raise AssertionError("commit should not run")


def _client(username, manager):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(history_routes.setup_history_routes(manager))
    return TestClient(app)


def _add_session(owner, name="Project", message_count=0):
    db = SessionLocal()
    try:
        session_id = str(uuid.uuid4())
        db.add(DbSession(
            id=session_id,
            name=name,
            endpoint_url="http://127.0.0.1:9",
            model=_MODEL,
            owner=owner,
            message_count=message_count,
        ))
        db.commit()
        return session_id
    finally:
        db.close()


def _add_message(session_id, content, when, role="assistant", meta=None, raw_meta=None):
    db = SessionLocal()
    try:
        msg_id = str(uuid.uuid4())
        if raw_meta is None:
            stored_meta = None if meta is None else json.dumps(meta)
        else:
            stored_meta = raw_meta
        db.add(DbChatMessage(
            id=msg_id,
            session_id=session_id,
            role=role,
            content=content,
            meta_data=stored_meta,
            timestamp=when,
        ))
        db.commit()
        return msg_id
    finally:
        db.close()


def _stored(msg_id):
    db = SessionLocal()
    try:
        row = db.query(DbChatMessage).filter(DbChatMessage.id == msg_id).one()
        return row.content, row.meta_data, row.session_id, row.role
    finally:
        db.close()


def _exists(msg_id):
    db = SessionLocal()
    try:
        return db.query(DbChatMessage.id).filter(DbChatMessage.id == msg_id).first() is not None
    finally:
        db.close()


def _stored_count(session_id):
    db = SessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == session_id).one()
        return row.message_count
    finally:
        db.close()


def _delete_rows(session_ids):
    db = SessionLocal()
    try:
        if session_ids:
            db.query(DbChatMessage).filter(
                DbChatMessage.session_id.in_(list(session_ids))
            ).delete(synchronize_session=False)
            db.query(DbSession).filter(DbSession.id.in_(list(session_ids))).delete(
                synchronize_session=False
            )
        db.commit()
    finally:
        db.close()


def _merge(username, session_id, manager, payload=None, raw=None):
    client = _client(username, manager)
    url = f"/api/session/{session_id}/merge-last-assistant"
    if raw is not None:
        return client.post(
            url,
            content=raw,
            headers={"Content-Type": "application/json"},
        )
    if payload is None:
        payload = {"separator": "\n\n"}
    return client.post(url, json=payload)


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert _LOG not in resp.text
    assert _LIST_GET not in resp.text
    assert _NULL_CONCAT not in resp.text
    _assert_no_secrets(resp)


def _memory(content, meta=None, role="assistant"):
    return ChatMessage(role=role, content=content, metadata=meta)


def _guard_db(monkeypatch):
    real = history_routes.SessionLocal
    state = {"opens": 0}

    def tracking():
        state["opens"] += 1
        return real()

    monkeypatch.setattr(history_routes, "SessionLocal", tracking)
    return state


def test_merge_last_assistant_joins_memory_and_replaces_db_rows():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=6)
    zero_id = _add_message(
        session_id, "zero", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "zero"}
    )
    first_id = _add_message(
        session_id,
        "first",
        datetime(2026, 1, 1, 0, 0, 2),
        meta={"keep": "yes", "dbonly": "x"},
    )
    notes_id = _add_message(
        session_id,
        "notes stay",
        datetime(2026, 1, 1, 0, 0, 3),
        role="user",
        meta={"keep": "notes"},
    )
    continue_id = _add_message(
        session_id,
        "the previous response was interrupted",
        datetime(2026, 1, 1, 0, 0, 4),
        role="user",
        meta={"keep": "continue"},
    )
    second_id = _add_message(
        session_id,
        "second",
        datetime(2026, 1, 1, 0, 0, 5),
        meta={"variantIndex": 1, "stopped": True},
    )
    after_id = _add_message(
        session_id,
        "after",
        datetime(2026, 1, 1, 0, 0, 6),
        role="user",
        meta={"keep": "after"},
    )
    foreign_session = _add_session(owner, name="Theirs", message_count=1)
    foreign_id = _add_message(
        foreign_session,
        "theirs",
        datetime(2026, 1, 1, 0, 0, 7),
        meta={"keep": "foreign"},
    )
    zero = _memory("zero", {"keep": "zero"})
    first = _memory("first", {"keep": "yes"})
    notes = _memory("notes stay", {"keep": "notes"}, role="user")
    continued = _memory(
        "the previous response was interrupted", {"keep": "continue"}, role="user"
    )
    second = _memory("second", {"variantIndex": 1, "stopped": True})
    after = _memory("after", {"keep": "after"}, role="user")
    memory = _MemSession([zero, first, notes, continued, second, after])
    manager = _Manager(session=memory)
    try:
        resp = _merge(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "merged": True}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert manager.saves == 1
        assert memory.history == [zero, first, notes, after]
        assert first.content == "first\n\nsecond"
        assert first.metadata == {"keep": "yes", "variantIndex": 1}
        assert "stopped" not in first.metadata
        assert zero.content == "zero"
        assert zero.metadata == {"keep": "zero"}
        assert notes.metadata == {"keep": "notes"}
        assert after.metadata == {"keep": "after"}
        assert continued not in memory.history
        assert second not in memory.history
        content, meta, stored_session, role = _stored(first_id)
        assert stored_session == session_id
        assert role == "assistant"
        assert content == "first\n\nsecond"
        assert json.loads(meta) == {"keep": "yes", "variantIndex": 1}
        assert "dbonly" not in json.loads(meta)
        assert "stopped" not in json.loads(meta)
        zero_content, zero_meta, _, zero_role = _stored(zero_id)
        assert zero_role == "assistant"
        assert zero_content == "zero"
        assert json.loads(zero_meta) == {"keep": "zero"}
        after_content, after_meta, _, after_role = _stored(after_id)
        assert after_role == "user"
        assert after_content == "after"
        assert json.loads(after_meta) == {"keep": "after"}
        assert _exists(notes_id) is False
        assert _exists(continue_id) is False
        assert _exists(second_id) is False
        foreign_content, foreign_meta, foreign_stored, _ = _stored(foreign_id)
        assert foreign_stored == foreign_session
        assert foreign_content == "theirs"
        assert json.loads(foreign_meta) == {"keep": "foreign"}
        assert _stored_count(session_id) == 6
        assert _stored_count(foreign_session) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id, foreign_session])


def test_merge_updates_dict_memory_and_lets_second_metadata_win():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    first_id = _add_message(
        session_id, "first", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes", "shared": "a"}
    )
    second_id = _add_message(
        session_id,
        "second",
        datetime(2026, 1, 1, 0, 0, 2),
        meta={"variantIndex": 1, "stopped": True, "shared": "b"},
    )
    first = {"role": "assistant", "content": "first", "metadata": {"keep": "yes", "shared": "a"}}
    second = {
        "role": "assistant",
        "content": "second",
        "metadata": {"variantIndex": 1, "stopped": True, "shared": "b"},
    }
    memory = _MemSession([first, second])
    manager = _Manager(session=memory)
    try:
        resp = _merge(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "merged": True}
        assert manager.calls == [session_id]
        assert manager.saves == 1
        assert memory.history == [first]
        assert first["content"] == "first\n\nsecond"
        assert first["metadata"] == {"keep": "yes", "variantIndex": 1, "shared": "b"}
        assert "stopped" not in first["metadata"]
        content, meta, _, _ = _stored(first_id)
        assert content == "first\n\nsecond"
        assert json.loads(meta) == {"keep": "yes", "variantIndex": 1, "shared": "b"}
        assert _exists(second_id) is False
        assert _stored_count(session_id) == 2
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_missing_separator_uses_default_newlines():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    first_id = _add_message(
        session_id, "first", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    second_id = _add_message(
        session_id, "second", datetime(2026, 1, 1, 0, 0, 2), meta={"variantIndex": 1}
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"variantIndex": 1})
    manager = _Manager(session=_MemSession([first, second]))
    try:
        resp = _merge(owner, session_id, manager, payload={})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "merged": True}
        assert manager.saves == 1
        assert first.content == "first\n\nsecond"
        assert first.metadata == {"keep": "yes", "variantIndex": 1}
        content, meta, _, _ = _stored(first_id)
        assert content == "first\n\nsecond"
        assert json.loads(meta) == {"keep": "yes", "variantIndex": 1}
        assert _exists(second_id) is False
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_empty_separator_is_used_as_is():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    first_id = _add_message(
        session_id, "first", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    second_id = _add_message(session_id, "second", datetime(2026, 1, 1, 0, 0, 2))
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", None)
    manager = _Manager(session=_MemSession([first, second]))
    try:
        resp = _merge(owner, session_id, manager, payload={"separator": ""})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "merged": True}
        assert manager.saves == 1
        assert first.content == "firstsecond"
        assert "stopped" not in first.metadata
        content, _, _, _ = _stored(first_id)
        assert content == "firstsecond"
        assert _exists(second_id) is False
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_fewer_than_two_memory_assistants_skip_db(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    older_id = _add_message(
        session_id, "older", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "older"}
    )
    newer_id = _add_message(
        session_id, "newer", datetime(2026, 1, 1, 0, 0, 2), meta={"keep": "newer"}
    )
    memory = _MemSession([])
    manager = _Manager(session=memory)
    guard = _guard_db(monkeypatch)
    histories = [
        [_memory("only", {"keep": "yes"})],
        [_memory("question", {"keep": "user"}, role="user")],
        [],
    ]
    try:
        for history in histories:
            memory.history = history
            snapshot = copy.deepcopy(history)
            resp = _merge(owner, session_id, manager)
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok", "merged": False}
            assert "error" not in resp.json()
            assert memory.history == snapshot or _same_messages(memory.history, snapshot)
            _assert_no_secrets(resp)
        assert manager.calls == [session_id, session_id, session_id]
        assert manager.saves == 0
        assert guard["opens"] == 0
        older_content, older_meta, _, _ = _stored(older_id)
        newer_content, newer_meta, _, _ = _stored(newer_id)
        assert older_content == "older"
        assert newer_content == "newer"
        assert json.loads(older_meta) == {"keep": "older"}
        assert json.loads(newer_meta) == {"keep": "newer"}
        assert _stored_count(session_id) == 2
    finally:
        _delete_rows([session_id])


def _same_messages(left, right):
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if isinstance(a, ChatMessage) or isinstance(b, ChatMessage):
            if not isinstance(a, ChatMessage) or not isinstance(b, ChatMessage):
                return False
            if (a.role, a.content, a.metadata) != (b.role, b.content, b.metadata):
                return False
        elif a != b:
            return False
    return True


def test_null_separator_with_one_assistant_is_success(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    other_id = _add_message(
        session_id, "other", datetime(2026, 1, 1, 0, 0, 2), meta={"keep": "other"}
    )
    only = _memory("only", {"keep": "yes"})
    memory = _MemSession([only])
    manager = _Manager(session=memory)
    guard = _guard_db(monkeypatch)
    try:
        resp = _merge(owner, session_id, manager, payload={"separator": None})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "merged": False}
        assert manager.calls == [session_id]
        assert manager.saves == 0
        assert guard["opens"] == 0
        assert memory.history == [only]
        assert only.content == "only"
        assert only.metadata == {"keep": "yes"}
        _, kept_meta, _, _ = _stored(kept_id)
        _, other_meta, _, _ = _stored(other_id)
        assert json.loads(kept_meta) == {"keep": "yes"}
        assert json.loads(other_meta) == {"keep": "other"}
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_null_separator_with_two_assistants_is_502(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    first_id = _add_message(
        session_id, "first", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    second_id = _add_message(
        session_id,
        "second",
        datetime(2026, 1, 1, 0, 0, 2),
        meta={"variantIndex": 1, "stopped": True},
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"variantIndex": 1, "stopped": True})
    memory = _MemSession([first, second])
    manager = _Manager(session=memory)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _merge(owner, session_id, manager, payload={"separator": None})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        assert memory.history == [first, second]
        assert first.content == "first"
        assert first.metadata == {"keep": "yes"}
        assert second.content == "second"
        assert second.metadata == {"variantIndex": 1, "stopped": True}
        first_content, first_meta, _, _ = _stored(first_id)
        second_content, second_meta, _, _ = _stored(second_id)
        assert first_content == "first"
        assert second_content == "second"
        assert json.loads(first_meta) == {"keep": "yes"}
        assert json.loads(second_meta) == {"variantIndex": 1, "stopped": True}
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _stored_count(session_id) == 2
    finally:
        _delete_rows([session_id])


def test_memory_merge_with_one_db_assistant_still_saves():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    only_id = _add_message(
        session_id, "only", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "only"}
    )
    user_id = _add_message(
        session_id,
        "question",
        datetime(2026, 1, 1, 0, 0, 2),
        role="user",
        meta={"keep": "user"},
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"variantIndex": 1, "stopped": True})
    memory = _MemSession([first, second])
    manager = _Manager(session=memory)
    try:
        resp = _merge(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "merged": True}
        assert manager.calls == [session_id]
        assert manager.saves == 1
        assert memory.history == [first]
        assert first.content == "first\n\nsecond"
        assert first.metadata == {"keep": "yes", "variantIndex": 1}
        only_content, only_meta, _, _ = _stored(only_id)
        user_content, user_meta, _, user_role = _stored(user_id)
        assert only_content == "only"
        assert json.loads(only_meta) == {"keep": "only"}
        assert user_role == "user"
        assert user_content == "question"
        assert json.loads(user_meta) == {"keep": "user"}
        assert _stored_count(session_id) == 2
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_no_db_assistant_still_saves():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    user_id = _add_message(
        session_id,
        "question",
        datetime(2026, 1, 1, 0, 0, 1),
        role="user",
        meta={"keep": "user"},
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"variantIndex": 1, "stopped": True})
    manager = _Manager(session=_MemSession([first, second]))
    try:
        resp = _merge(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "merged": True}
        assert manager.saves == 1
        assert first.content == "first\n\nsecond"
        assert first.metadata == {"keep": "yes", "variantIndex": 1}
        _, user_meta, _, user_role = _stored(user_id)
        assert user_role == "user"
        assert json.loads(user_meta) == {"keep": "user"}
        assert _stored_count(session_id) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_invalid_json_is_502_before_session(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"variantIndex": 1, "stopped": True})
    memory = _MemSession([first, second])
    manager = _Manager(session=memory)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _merge(
            owner,
            session_id,
            manager,
            raw=f"{_JSON_SECRET} not-json".encode(),
        )
        _assert_fixed(resp)
        assert manager.calls == []
        assert manager.saves == 0
        assert memory.history == [first, second]
        assert first.content == "first"
        assert second.metadata["stopped"] is True
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _stored_count(session_id) == 2
    finally:
        _delete_rows([session_id])


def test_json_list_is_502_before_session(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"stopped": True})
    memory = _MemSession([first, second])
    manager = _Manager(session=memory)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _merge(owner, session_id, manager, payload=[_JSON_SECRET])
        _assert_fixed(resp)
        assert manager.calls == []
        assert manager.saves == 0
        assert memory.history == [first, second]
        assert first.metadata == {"keep": "yes"}
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _stored_count(session_id) == 2
    finally:
        _delete_rows([session_id])


def test_query_failure_leaves_memory_merge(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=3)
    first_id = _add_message(
        session_id, "first", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    continue_id = _add_message(
        session_id,
        "the previous response was interrupted",
        datetime(2026, 1, 1, 0, 0, 2),
        role="user",
        meta={"keep": "continue"},
    )
    second_id = _add_message(
        session_id,
        "second",
        datetime(2026, 1, 1, 0, 0, 3),
        meta={"variantIndex": 1, "stopped": True},
    )
    first = _memory("first", {"keep": "yes"})
    continued = _memory(
        "the previous response was interrupted", {"keep": "continue"}, role="user"
    )
    second = _memory("second", {"variantIndex": 1, "stopped": True})
    memory = _MemSession([first, continued, second])
    manager = _Manager(session=memory)
    failed = _FailSession(RuntimeError(_QUERY_SECRET))
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: failed)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _merge(owner, session_id, manager)
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        assert failed.closed is True
        assert failed.rollbacks == 0
        assert memory.history == [first]
        assert first.content == "first\n\nsecond"
        assert first.metadata == {"keep": "yes", "variantIndex": 1}
        first_content, first_meta, _, _ = _stored(first_id)
        continue_content, continue_meta, _, continue_role = _stored(continue_id)
        second_content, second_meta, _, _ = _stored(second_id)
        assert first_content == "first"
        assert json.loads(first_meta) == {"keep": "yes"}
        assert continue_role == "user"
        assert continue_content == "the previous response was interrupted"
        assert json.loads(continue_meta) == {"keep": "continue"}
        assert second_content == "second"
        assert json.loads(second_meta) == {"variantIndex": 1, "stopped": True}
        assert _QUERY_SECRET in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _stored_count(session_id) == 3
    finally:
        _delete_rows([session_id])


def test_http_exception_is_reraised(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    first_id = _add_message(
        session_id, "first", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    second_id = _add_message(
        session_id,
        "second",
        datetime(2026, 1, 1, 0, 0, 2),
        meta={"variantIndex": 1, "stopped": True},
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"variantIndex": 1, "stopped": True})
    memory = _MemSession([first, second])
    manager = _Manager(session=memory)
    failed = _FailSession(HTTPException(status_code=409, detail="Session assistant merge refused"))
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: failed)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _merge(owner, session_id, manager)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Session assistant merge refused"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert manager.saves == 0
        assert failed.closed is True
        assert failed.rollbacks == 0
        assert memory.history == [first]
        assert first.content == "first\n\nsecond"
        assert first.metadata == {"keep": "yes", "variantIndex": 1}
        first_content, first_meta, _, _ = _stored(first_id)
        second_content, second_meta, _, _ = _stored(second_id)
        assert first_content == "first"
        assert json.loads(first_meta) == {"keep": "yes"}
        assert second_content == "second"
        assert json.loads(second_meta) == {"variantIndex": 1, "stopped": True}
        _assert_no_secrets(resp)
        assert _LOG not in caplog.text
        assert _FIXED_ERROR not in caplog.text
        assert _stored_count(session_id) == 2
    finally:
        _delete_rows([session_id])


def test_get_session_failure_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"stopped": True})
    memory = _MemSession([first, second])
    manager = _Manager(session=memory, exc=RuntimeError(_MERGE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _merge(owner, session_id, manager)
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        assert memory.history == [first, second]
        assert first.content == "first"
        assert second.metadata == {"stopped": True}
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert _MERGE_SECRET in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _stored_count(session_id) == 2
    finally:
        _delete_rows([session_id])


def test_keyerror_hides_message(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"stopped": True})
    memory = _MemSession([first, second])
    manager = _Manager(session=memory, exc=KeyError(_KEY_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _merge(owner, session_id, manager)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert manager.saves == 0
        assert memory.history == [first, second]
        assert first.content == "first"
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        _assert_no_secrets(resp)
        assert _LOG not in caplog.text
        assert _FIXED_ERROR not in caplog.text
        assert _KEY_SECRET not in caplog.text
        assert _stored_count(session_id) == 2
    finally:
        _delete_rows([session_id])


def test_save_failure_hides_exception_after_commit(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    first_id = _add_message(
        session_id, "first", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    second_id = _add_message(
        session_id,
        "second",
        datetime(2026, 1, 1, 0, 0, 2),
        meta={"variantIndex": 1, "stopped": True},
    )
    first = _memory("first", {"keep": "yes"})
    second = _memory("second", {"variantIndex": 1, "stopped": True})
    memory = _MemSession([first, second])
    manager = _Manager(
        session=memory, save_exc=RuntimeError(_SAVE_SECRET)
    )
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _merge(owner, session_id, manager)
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 1
        assert memory.history == [first]
        assert first.content == "first\n\nsecond"
        assert first.metadata == {"keep": "yes", "variantIndex": 1}
        content, meta, _, _ = _stored(first_id)
        assert content == "first\n\nsecond"
        assert json.loads(meta) == {"keep": "yes", "variantIndex": 1}
        assert _exists(second_id) is False
        assert _stored_count(session_id) == 2
        assert _SAVE_SECRET in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_other_owner_null_owner_and_missing_row_stay_owner_404(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    theirs = _add_session(stranger, name="Theirs")
    legacy = _add_session(None, name="Legacy")
    missing = str(uuid.uuid4())
    manager = _Manager()
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    raw = f"{_JSON_SECRET} not-json".encode()
    try:
        other = _merge(owner, theirs, manager, raw=raw)
        blank = _merge(owner, legacy, manager, raw=raw)
        gone = _merge(owner, missing, manager, raw=raw)
        assert other.status_code == 404
        assert other.json()["detail"] == f"Session {theirs} not found"
        assert blank.status_code == 404
        assert blank.json()["detail"] == f"Session {legacy} not found"
        assert gone.status_code == 404
        assert gone.json()["detail"] == f"Session {missing} not found"
        assert "error" not in other.json()
        assert "error" not in blank.json()
        assert "error" not in gone.json()
        assert manager.calls == []
        assert manager.saves == 0
        _assert_no_secrets(other)
        _assert_no_secrets(blank)
        _assert_no_secrets(gone)
        assert _JSON_SECRET not in caplog.text
        assert _LOG not in caplog.text
    finally:
        _delete_rows([theirs, legacy])


def test_anonymous_is_403():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager()
    try:
        resp = _merge(None, session_id, manager, raw=f"{_JSON_SECRET} not-json".encode())
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert manager.calls == []
        assert manager.saves == 0
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_merge_keeps_the_fixed_sentence_and_leaves_neighbors():
    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(message: str, status_code: int = 502, **extra):" in history
    assert history.count("def history_error(") == 1
    history_start = history.index("    async def get_session_history(")
    truncate_start = history.index("    async def truncate_session(")
    add_start = history.index("    async def add_message(")
    delete_start = history.index("    async def delete_messages(")
    edit_start = history.index("    async def edit_message(")
    mark_start = history.index("    async def mark_stopped(")
    update_start = history.index("    async def update_last_meta(")
    merge_start = history.index("    async def merge_last_assistant(")
    fork_start = history.index("    async def fork_session(")
    topics_start = history.index("    async def get_conversation_topics(")
    loaded = history[history_start:truncate_start]
    truncate = history[truncate_start:add_start]
    added = history[add_start:delete_start]
    deleted = history[delete_start:edit_start]
    edited = history[edit_start:mark_start]
    marked = history[mark_start:update_start]
    updated = history[update_start:merge_start]
    merged = history[merge_start:fork_start]
    forked = history[fork_start:topics_start]

    assert "history_error" not in loaded
    assert 'logger.error(f"DB fallback failed for {session_id}: {e}")' in loaded
    assert "SessionLocal" in loaded

    assert "history_error" not in truncate
    assert "except HTTPException:" not in truncate
    assert 'logger.error(f"Truncate error {session_id}: {e}")' in truncate
    assert "raise HTTPException(500, str(e))" in truncate
    assert 'HTTPException(404, "Session not found")' in truncate
    assert 'return {"status": "ok", "kept": keep_count, "truncated": result}' in truncate
    assert "db.rollback()" not in truncate
    assert "SessionLocal" not in truncate
    owner_at = truncate.index("_verify_session_owner(request, session_id)")
    try_at = truncate.index("try:")
    assert owner_at < try_at

    assert "history_error" not in added
    assert "except HTTPException:" not in added
    assert 'raise HTTPException(400, "content is required")' in added
    assert "except KeyError:" in added
    assert 'raise HTTPException(404, "Session not found")' in added
    assert 'return {"status": "ok"}' in added
    assert "{e}" not in added
    assert "str(e)" not in added

    assert "history_error" not in deleted
    assert "except HTTPException:" not in deleted
    assert 'logger.error(f"Delete messages error {session_id}: {e}")' in deleted
    assert "raise HTTPException(500, str(e))" in deleted
    assert 'HTTPException(404, "Session not found")' in deleted
    assert 'return {"status": "ok", "deleted": deleted}' in deleted
    assert 'return {"status": "ok", "deleted": 0}' in deleted
    assert "db.close()" in deleted
    assert "SessionLocal" in deleted
    assert "db.rollback()" not in deleted
    owner_at = deleted.index("_verify_session_owner(request, session_id)")
    try_at = deleted.index("try:")
    assert owner_at < try_at

    assert "history_error" not in edited
    assert "except HTTPException:" in edited
    assert 'logger.error(f"Edit message error {session_id}: {e}")' in edited
    assert "raise HTTPException(500, str(e))" in edited
    assert 'HTTPException(404, "Session not found")' in edited
    assert 'raise HTTPException(400, "msg_id and content are required")' in edited
    assert 'raise HTTPException(404, "Message not found")' in edited
    assert 'return {"status": "ok"}' in edited
    assert "db.close()" in edited
    assert "SessionLocal" in edited
    assert "db.rollback()" not in edited
    assert "save_sessions()" not in edited
    owner_at = edited.index("_verify_session_owner(request, session_id)")
    try_at = edited.index("try:")
    assert owner_at < try_at

    assert "history_error" not in marked
    assert "except HTTPException:" not in marked
    assert 'logger.error(f"Mark stopped error {session_id}: {e}")' in marked
    assert "raise HTTPException(500, str(e))" in marked
    assert 'HTTPException(404, "Session not found")' in marked
    assert 'return {"status": "ok"}' in marked
    assert "db.close()" in marked
    assert "SessionLocal" in marked
    assert "save_sessions()" in marked
    assert "db.rollback()" not in marked
    assert "request.json" not in marked
    owner_at = marked.index("_verify_session_owner(request, session_id)")
    try_at = marked.index("try:")
    assert owner_at < try_at

    assert "history_error" not in updated
    assert "except HTTPException:" not in updated
    assert 'logger.error(f"Update last meta error {session_id}: {e}")' in updated
    assert "raise HTTPException(500, str(e))" in updated
    assert 'HTTPException(404, "Session not found")' in updated
    assert 'return {"status": "ok"}' in updated
    assert "db.close()" in updated
    assert "SessionLocal" in updated
    assert "save_sessions()" in updated
    assert "request.json()" in updated
    assert 'body.get("metadata", {})' in updated
    assert "db.rollback()" not in updated
    owner_at = updated.index("_verify_session_owner(request, session_id)")
    try_at = updated.index("try:")
    assert owner_at < try_at

    owner_at = merged.index("_verify_session_owner(request, session_id)")
    try_at = merged.index("try:")
    assert owner_at < try_at
    assert "except HTTPException:" in merged
    assert 'history_error("Failed to merge assistant messages")' in merged
    assert 'logger.exception("Session assistant merge failed for %s", session_id)' in merged
    assert 'HTTPException(404, "Session not found")' in merged
    assert 'return {"status": "ok", "merged": False}' in merged
    assert 'return {"status": "ok", "merged": True}' in merged
    assert "db.close()" in merged
    assert "SessionLocal" in merged
    assert "save_sessions()" in merged
    assert "request.json()" in merged
    assert 'body.get("separator", "\\n\\n")' in merged
    assert "merged_meta.pop('stopped', None)" in merged
    assert "content1 + separator + content2" in merged
    assert "msg1.content = merged_content" in merged
    assert "msg1['content'] = merged_content" in merged
    assert "{e}" not in merged
    assert "str(e)" not in merged
    assert "HTTPException(500" not in merged
    assert "db.rollback()" not in merged
    assert "session.model" not in merged
    assert 'logger.error(f"Merge assistant error {session_id}: {e}")' not in merged
    assert merged.index("request.json()") < merged.index("get_session(")
    assert merged.index('return {"status": "ok", "merged": False}') < merged.index("db = SessionLocal()")
    assert merged.index("content1 + separator + content2") < merged.index("msg1.content = merged_content")
    assert merged.index("content1 + separator + content2") < merged.index("db = SessionLocal()")
    assert merged.index("db.close()") < merged.index("session_manager.save_sessions()")
    assert merged.index("session_manager.save_sessions()") < merged.index('return {"status": "ok", "merged": True}')
    key_at = merged.index("except KeyError:")
    http_at = merged.index("except HTTPException:")
    exc_at = merged.index("except Exception:")
    assert key_at < http_at < exc_at
    assert history.count('history_error("Failed to merge assistant messages")') == 1
    assert 'history_error("Failed to update message metadata")' not in history
    assert 'history_error("Failed to mark message stopped")' not in history

    assert "history_error" not in forked
    assert "except HTTPException:" in forked
    assert 'logger.error(f"Fork error {session_id}: {e}")' in forked
    assert "raise HTTPException(500, str(e))" in forked
    assert "request.json()" in forked
    assert 'body.get("keep_count", 0)' in forked
    assert "db.rollback()" not in forked
    owner_at = forked.index("_verify_session_owner(request, session_id)")
    try_at = forked.index("try:")
    assert owner_at < try_at

    chat = (_REPO / "static/js/chat.js").read_text(encoding="utf-8")
    edit_ai_start = chat.index("  export async function editAIMessage(")
    rewrite_start = chat.index("  export async function rewriteWith(")
    continue_start = chat.index("  export async function continueFrom(")
    edit_client = chat[edit_ai_start:rewrite_start]
    rewrite = chat[rewrite_start:continue_start]
    assert "method: 'POST'" in edit_client
    assert "api/session/${sessionId}/edit-message" in edit_client
    assert "res.ok" in edit_client
    assert "Server error " in edit_client
    assert "Edit failed: " in edit_client
    assert ".json(" not in edit_client
    assert ".text(" not in edit_client
    assert "mark-stopped" not in edit_client
    assert "update-last-meta" not in edit_client

    mark_lines = [line for line in chat.splitlines() if "mark-stopped" in line]
    assert len(mark_lines) == 2
    assert chat.count("mark-stopped failed:") == 2
    for line in mark_lines:
        assert "method: 'POST'" in line
        assert "mark-stopped failed:" in line
        assert ".json(" not in line
        assert ".text(" not in line
        assert "res.ok" not in line

    assert chat.count("update-last-meta (variants) failed:") == 1
    assert chat.count("update-last-meta (variantIndex) failed:") == 1
    assert chat.count("update-last-meta") == 5
    assert chat.count("merge-last-assistant failed:") == 1
    fetch_at = chat.index("fetch(`${API_BASE}/api/session/${sid}/merge-last-assistant`")
    warn_at = chat.index("merge-last-assistant failed:", fetch_at)
    block = chat[fetch_at:warn_at]
    assert "method: 'POST'" in block
    assert "JSON.stringify({ separator: '\\n\\n*(continued)*\\n\\n' })" in block
    assert "res.ok" not in block
    assert ".json(" not in block
    assert ".text(" not in block
    method_lines = [line for line in block.splitlines() if "method: 'POST'" in line]
    assert len(method_lines) == 1
    assert "merge-last-assistant failed:" not in method_lines[0]
    warn_line = chat.splitlines()[chat[:warn_at].count("\n")]
    assert "merge-last-assistant failed:" in warn_line
    assert "method: 'POST'" not in warn_line

    first = chat.index("fetch(`${API_BASE}/api/session/${sid}/update-last-meta`")
    variants_end = chat.index("update-last-meta (variants) failed:", first)
    variants = chat[first:variants_end]
    second = chat.index("fetch(`${API_BASE}/api/session/${sid}/update-last-meta`", variants_end)
    index_end = chat.index("update-last-meta (variantIndex) failed:", second)
    variant_index = chat[second:index_end]
    for block in (variants, variant_index):
        assert "method: 'POST'" in block
        assert "JSON.stringify" in block
        assert "res.ok" not in block
        assert ".json(" not in block
        assert ".text(" not in block

    meta_at = rewrite.index("api/session/${sessionId}/update-last-meta")
    meta_end = rewrite.index("catch (_) {}", meta_at)
    meta_slice = rewrite[meta_at:meta_end + len("catch (_) {}")]
    assert "method: 'POST'" in meta_slice
    assert "JSON.stringify" in meta_slice
    assert "await fetch(" in rewrite[max(0, meta_at - 40):meta_at]
    assert "res.ok" not in meta_slice
    assert ".json(" not in meta_slice
    assert ".text(" not in meta_slice
    assert "Rewrite failed:" not in meta_slice
    assert rewrite.count("api/session/${sessionId}/update-last-meta") == 1
    assert "Rewrite failed: " in rewrite
    assert "err.message" in rewrite
    assert rewrite.index("api/session/${sessionId}/update-last-meta") < rewrite.index("Rewrite failed: ")

    fork_fn = chat.index("  export async function forkFrom(")
    fork_end = chat.index("\n  export async function ", fork_fn + 10)
    fork_client = chat[fork_fn:fork_end]
    assert "api/session/${sessionId}/fork" in fork_client
    assert "await res.text()" in fork_client
    assert "res.ok" in fork_client
    assert "const data = await res.json()" in fork_client
    assert "Fork failed: " in fork_client
    assert "history_error" not in fork_client

    slash = (_REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
    assert "api/session/${ctx.sid}/fork" in slash
    assert "slashReply('Fork failed')" in slash

    skills = (_REPO / "routes/skills_routes.py").read_text(encoding="utf-8")
    assert 'return {"builtin": [], "count": 0, "error": str(e)}' in skills
    assert "raise HTTPException(500, str(e))" in skills
    assert "def skill_error(" not in skills
    assert "history_error" not in skills

    upload = (_REPO / "routes/upload_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(500, f"Vision analysis failed: {e}")' in upload
    assert "def vision_error(" not in upload
    assert "history_error" not in upload

    docs = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    fill_start = docs.index("    async def ai_fill_annotations(")
    render_start = docs.index("    async def render_pdf(", fill_start)
    fill = docs[fill_start:render_start]
    assert "import fitz" in fill
    assert "No vision model available: {e}" in fill
    assert "document_error" not in fill
    assert "history_error" not in fill

    drafts = (_REPO / "routes/editor_draft_routes.py").read_text(encoding="utf-8")
    draft_delete = drafts.index("    async def delete_draft(")
    draft_end = drafts.index("\n    return router", draft_delete)
    draft_slice = drafts[draft_delete:draft_end]
    assert "raise HTTPException(500, str(e))" in draft_slice
    assert "def draft_error(" not in drafts
    assert "history_error" not in drafts
    assert 'raise HTTPException(500, "Could not save draft")' in drafts
    assert 'logger.warning(f"editor-draft create failed: {e}")' in drafts
    assert 'raise HTTPException(500, "Could not update draft")' in drafts
    assert 'logger.warning(f"editor-draft update failed: {e}")' in drafts
