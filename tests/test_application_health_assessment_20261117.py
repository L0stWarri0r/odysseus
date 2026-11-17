"""POST /api/session/{id}/update-last-meta must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to update message metadata"}.
The exception is logged and is not returned. An HTTPException raised inside
the handler is still passed through. A missing in-memory session stays
HTTP 404 "Session not found". Invalid JSON and a JSON list fail before
session lookup and do not save. A missing metadata key is a success and
keeps existing keys. Success stays {"status": "ok"}.

The in-memory merge happens before the database query. A query failure
leaves that memory change in place, does not roll back, and does not save
sessions. save_sessions() runs only after the database block, once, on the
success path and also when save_sessions itself raises. Ownership is still
checked before the try. History read, truncate, add-message, delete-messages,
edit-message, and mark-stopped were not changed. The chat client does not
read the body.
"""
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

_UPDATE_SECRET = "UPDATE_SECRET session metadata update token=do-not-return"
_KEY_SECRET = "KEY_SECRET session missing token=do-not-return"
_JSON_SECRET = "JSON_SECRET session body token=do-not-return"
_QUERY_SECRET = "QUERY_SECRET session query token=do-not-return"
_SAVE_SECRET = "SAVE_SECRET session save token=do-not-return"
_FIXED_ERROR = "Failed to update message metadata"
_SECRETS = (_UPDATE_SECRET, _KEY_SECRET, _JSON_SECRET, _QUERY_SECRET, _SAVE_SECRET)
_REPO = Path(__file__).resolve().parent.parent
_MODEL = "test-model"
_NULL_ITER = "'NoneType' object is not iterable"
_NULL_UPDATE = "'NoneType' object has no attribute 'update'"
_LIST_GET = "'list' object has no attribute 'get'"


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


def _update(username, session_id, manager, payload=None, raw=None):
    client = _client(username, manager)
    url = f"/api/session/{session_id}/update-last-meta"
    if raw is not None:
        return client.post(
            url,
            content=raw,
            headers={"Content-Type": "application/json"},
        )
    if payload is None:
        payload = {"metadata": {"variantIndex": 1}}
    return client.post(url, json=payload)


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    assert "Session metadata update failed" not in resp.text
    assert _NULL_ITER not in resp.text
    assert _NULL_UPDATE not in resp.text
    assert _LIST_GET not in resp.text
    _assert_no_secrets(resp)


def _memory(content, meta=None, role="assistant"):
    return ChatMessage(role=role, content=content, metadata=meta)


def test_update_last_meta_merges_db_and_matching_memory():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=4)
    older_id = _add_message(
        session_id, "older", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "older"}
    )
    latest_id = _add_message(
        session_id, "latest", datetime(2026, 1, 1, 0, 0, 2), meta={"keep": "yes"}
    )
    user_id = _add_message(
        session_id,
        "question",
        datetime(2026, 1, 1, 0, 0, 3),
        role="user",
        meta={"keep": "user"},
    )
    foreign_session = _add_session(owner, name="Theirs", message_count=1)
    foreign_id = _add_message(
        foreign_session,
        "theirs",
        datetime(2026, 1, 1, 0, 0, 4),
        meta={"keep": "foreign"},
    )
    older = _memory("older", {"keep": "older"})
    latest = _memory("latest", {"keep": "yes"})
    user = _memory("question", {"keep": "user"}, role="user")
    memory = _MemSession([older, latest, user])
    manager = _Manager(session=memory)
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert manager.saves == 1
        _, latest_meta, stored_session, latest_role = _stored(latest_id)
        assert stored_session == session_id
        assert latest_role == "assistant"
        assert json.loads(latest_meta) == {"keep": "yes", "variantIndex": 1}
        assert "stopped" not in json.loads(latest_meta)
        assert "model" not in json.loads(latest_meta)
        _, older_meta, _, _ = _stored(older_id)
        assert json.loads(older_meta) == {"keep": "older"}
        _, user_meta, _, user_role = _stored(user_id)
        assert user_role == "user"
        assert json.loads(user_meta) == {"keep": "user"}
        _, foreign_meta, foreign_stored, _ = _stored(foreign_id)
        assert foreign_stored == foreign_session
        assert json.loads(foreign_meta) == {"keep": "foreign"}
        assert older.metadata == {"keep": "older"}
        assert latest.metadata == {"keep": "yes", "variantIndex": 1}
        assert user.metadata == {"keep": "user"}
        assert _stored_count(session_id) == 4
        assert _stored_count(foreign_session) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id, foreign_session])


def test_update_last_meta_updates_dict_memory():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    earlier = _memory("earlier", {"keep": "earlier"})
    matched = {"role": "assistant", "content": "answer", "metadata": {"keep": "yes"}}
    manager = _Manager(session=_MemSession([earlier, matched]))
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert manager.calls == [session_id]
        assert manager.saves == 1
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes", "variantIndex": 1}
        assert matched["metadata"] == {"keep": "yes", "variantIndex": 1}
        assert earlier.metadata == {"keep": "earlier"}
        assert _stored_count(session_id) == 2
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_dict_without_metadata_key_receives_update():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(session_id, "answer", datetime(2026, 1, 1, 0, 0, 1))
    matched = {"role": "assistant", "content": "answer"}
    manager = _Manager(session=_MemSession([matched]))
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert manager.saves == 1
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"variantIndex": 1}
        assert matched["metadata"] == {"variantIndex": 1}
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_missing_metadata_key_keeps_existing():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("answer", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    try:
        resp = _update(owner, session_id, manager, payload={})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert manager.saves == 1
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes"}
        assert _stored_count(session_id) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_empty_metadata_object_keeps_existing():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("answer", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    try:
        resp = _update(owner, session_id, manager, payload={"metadata": {}})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert manager.calls == [session_id]
        assert manager.saves == 1
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes"}
        assert _stored_count(session_id) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_null_metadata_replaces_empty_chatmessage(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("answer", None)
    manager = _Manager(session=_MemSession([matched]))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager, payload={"metadata": None})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {}
        assert f"Session metadata update failed for {session_id}" in caplog.text
        assert _stored_count(session_id) == 1
    finally:
        _delete_rows([session_id])


def test_null_metadata_leaves_existing_chatmessage_dict(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("answer", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager, payload={"metadata": None})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes"}
        assert _stored_count(session_id) == 1
    finally:
        _delete_rows([session_id])


def test_null_metadata_sets_missing_dict_key(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = {"role": "assistant", "content": "answer"}
    manager = _Manager(session=_MemSession([matched]))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager, payload={"metadata": None})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched["metadata"] == {}
        assert _stored_count(session_id) == 1
    finally:
        _delete_rows([session_id])


def test_null_metadata_leaves_existing_dict_entry(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = {"role": "assistant", "content": "answer", "metadata": {"keep": "yes"}}
    manager = _Manager(session=_MemSession([matched]))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager, payload={"metadata": None})
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched["metadata"] == {"keep": "yes"}
        assert _stored_count(session_id) == 1
    finally:
        _delete_rows([session_id])


def test_no_memory_assistant_still_updates_db():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    user = _memory("question", {"keep": "user"}, role="user")
    manager = _Manager(session=_MemSession([user]))
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert manager.calls == [session_id]
        assert manager.saves == 1
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes", "variantIndex": 1}
        assert user.metadata == {"keep": "user"}
        assert _stored_count(session_id) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_missing_db_assistant_still_saves():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    user_id = _add_message(
        session_id,
        "question",
        datetime(2026, 1, 1, 0, 0, 2),
        role="user",
        meta={"keep": "user"},
    )
    matched = _memory("answer", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert manager.calls == [session_id]
        assert manager.saves == 1
        assert matched.metadata == {"keep": "yes", "variantIndex": 1}
        _, user_meta, _, user_role = _stored(user_id)
        assert user_role == "user"
        assert json.loads(user_meta) == {"keep": "user"}
        assert _stored_count(session_id) == 1
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_no_assistant_anywhere_still_succeeds():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=0)
    user = _memory("question", {"keep": "user"}, role="user")
    manager = _Manager(session=_MemSession([user]))
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert manager.saves == 1
        assert user.metadata == {"keep": "user"}
        assert _stored_count(session_id) == 0
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_invalid_stored_metadata_is_replaced():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), raw_meta="not-json"
    )
    matched = _memory("answer", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert manager.saves == 1
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"variantIndex": 1}
        assert matched.metadata == {"keep": "yes", "variantIndex": 1}
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_null_stored_metadata_is_502(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), raw_meta="null"
    )
    matched = _memory("answer", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager)
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        _, meta, _, _ = _stored(msg_id)
        assert meta == "null"
        assert matched.metadata == {"keep": "yes", "variantIndex": 1}
        assert f"Session metadata update failed for {session_id}" in caplog.text
        assert _stored_count(session_id) == 1
    finally:
        _delete_rows([session_id])


def test_invalid_json_is_502_before_session(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("kept", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(
            owner,
            session_id,
            manager,
            raw=f"{_JSON_SECRET} not-json".encode(),
        )
        _assert_fixed(resp)
        assert manager.calls == []
        assert manager.saves == 0
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes"}
        assert f"Session metadata update failed for {session_id}" in caplog.text
        assert _stored_count(session_id) == 1
    finally:
        _delete_rows([session_id])


def test_json_list_is_502_before_session(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("kept", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager, payload=[_JSON_SECRET])
        _assert_fixed(resp)
        assert manager.calls == []
        assert manager.saves == 0
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes"}
        assert f"Session metadata update failed for {session_id}" in caplog.text
        assert _stored_count(session_id) == 1
    finally:
        _delete_rows([session_id])


def test_query_failure_merges_memory_and_hides_exception(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=4)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("kept", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    failed = _FailSession(RuntimeError(_QUERY_SECRET))
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: failed)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager)
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        assert failed.closed is True
        assert failed.rollbacks == 0
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes", "variantIndex": 1}
        assert _stored_count(session_id) == 4
        assert _QUERY_SECRET in caplog.text
        assert f"Session metadata update failed for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_get_session_failure_hides_exception(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("kept", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]), exc=RuntimeError(_UPDATE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager)
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 0
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes"}
        assert _UPDATE_SECRET in caplog.text
        assert f"Session metadata update failed for {session_id}" in caplog.text
        assert _stored_count(session_id) == 1
    finally:
        _delete_rows([session_id])


def test_keyerror_hides_message(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("kept", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]), exc=KeyError(_KEY_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert manager.saves == 0
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes"}
        _assert_no_secrets(resp)
        assert "Session metadata update failed" not in caplog.text
        assert _FIXED_ERROR not in caplog.text
        assert _KEY_SECRET not in caplog.text
    finally:
        _delete_rows([session_id])


def test_http_exception_is_reraised(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    kept_id = _add_message(
        session_id, "kept", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("kept", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]))
    failed = _FailSession(HTTPException(status_code=409, detail="Session metadata update refused"))
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: failed)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Session metadata update refused"
        assert "error" not in resp.json()
        assert manager.calls == [session_id]
        assert manager.saves == 0
        assert failed.closed is True
        assert failed.rollbacks == 0
        _, meta, _, _ = _stored(kept_id)
        assert json.loads(meta) == {"keep": "yes"}
        assert matched.metadata == {"keep": "yes", "variantIndex": 1}
        _assert_no_secrets(resp)
        assert "Session metadata update failed" not in caplog.text
        assert _FIXED_ERROR not in caplog.text
    finally:
        _delete_rows([session_id])


def test_save_failure_hides_exception_after_commit(caplog):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    msg_id = _add_message(
        session_id, "answer", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "yes"}
    )
    matched = _memory("answer", {"keep": "yes"})
    manager = _Manager(session=_MemSession([matched]), save_exc=RuntimeError(_SAVE_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _update(owner, session_id, manager)
        _assert_fixed(resp)
        assert manager.calls == [session_id]
        assert manager.saves == 1
        _, meta, _, _ = _stored(msg_id)
        assert json.loads(meta) == {"keep": "yes", "variantIndex": 1}
        assert matched.metadata == {"keep": "yes", "variantIndex": 1}
        assert _stored_count(session_id) == 1
        assert _SAVE_SECRET in caplog.text
        assert f"Session metadata update failed for {session_id}" in caplog.text
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
        other = _update(owner, theirs, manager, raw=raw)
        blank = _update(owner, legacy, manager, raw=raw)
        gone = _update(owner, missing, manager, raw=raw)
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
        assert "Session metadata update failed" not in caplog.text
    finally:
        _delete_rows([theirs, legacy])


def test_anonymous_is_403():
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager()
    try:
        resp = _update(None, session_id, manager, raw=f"{_JSON_SECRET} not-json".encode())
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert manager.calls == []
        assert manager.saves == 0
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_update_last_meta_keeps_the_fixed_sentence_and_leaves_neighbors():
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
    loaded = history[history_start:truncate_start]
    truncate = history[truncate_start:add_start]
    added = history[add_start:delete_start]
    deleted = history[delete_start:edit_start]
    edited = history[edit_start:mark_start]
    marked = history[mark_start:update_start]
    updated = history[update_start:merge_start]
    merged = history[merge_start:fork_start]

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

    owner_at = updated.index("_verify_session_owner(request, session_id)")
    try_at = updated.index("try:")
    assert owner_at < try_at
    assert "except HTTPException:" in updated
    assert 'history_error("Failed to update message metadata")' in updated
    assert 'logger.exception("Session metadata update failed for %s", session_id)' in updated
    assert 'HTTPException(404, "Session not found")' in updated
    assert 'return {"status": "ok"}' in updated
    assert "db.close()" in updated
    assert "SessionLocal" in updated
    assert "save_sessions()" in updated
    assert "request.json()" in updated
    assert "body.get(\"metadata\", {})" in updated
    assert "if not msg.metadata:" in updated
    assert "msg.metadata.update(meta_update)" in updated
    assert "if 'metadata' not in msg:" in updated
    assert "msg['metadata'].update(meta_update)" in updated
    assert "meta.update(meta_update)" in updated
    assert "{e}" not in updated
    assert "str(e)" not in updated
    assert "HTTPException(500" not in updated
    assert "db.rollback()" not in updated
    assert "session.model" not in updated
    assert "stopped" not in updated
    assert 'logger.error(f"Update last meta error {session_id}: {e}")' not in updated
    assert updated.index("request.json()") < updated.index("get_session(")
    assert updated.index("if not msg.metadata:") < updated.index("msg.metadata.update(meta_update)")
    assert updated.index("msg.metadata.update(meta_update)") < updated.index("db = SessionLocal()")
    assert updated.index("db.close()") < updated.index("session_manager.save_sessions()")
    assert updated.index("session_manager.save_sessions()") < updated.index('return {"status": "ok"}')
    key_at = updated.index("except KeyError:")
    http_at = updated.index("except HTTPException:")
    exc_at = updated.index("except Exception:")
    assert key_at < http_at < exc_at
    assert history.count('history_error("Failed to update message metadata")') == 1

    assert "history_error" not in merged
    assert "except HTTPException:" not in merged
    assert 'logger.error(f"Merge assistant error {session_id}: {e}")' in merged
    assert "raise HTTPException(500, str(e))" in merged
    assert 'HTTPException(404, "Session not found")' in merged
    assert 'return {"status": "ok", "merged": False}' in merged
    assert 'return {"status": "ok", "merged": True}' in merged
    assert "db.close()" in merged
    assert "SessionLocal" in merged
    assert "save_sessions()" in merged
    assert "request.json()" in merged
    assert "db.rollback()" not in merged
    owner_at = merged.index("_verify_session_owner(request, session_id)")
    try_at = merged.index("try:")
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
