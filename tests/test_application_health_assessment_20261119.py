"""POST /api/session/{id}/fork must not return exception text.

An unexpected failure is HTTP 502 {"error": "Failed to fork session"}.
The exception is logged and is not returned. An HTTPException raised inside
the handler is still passed through. A missing in-memory session stays
HTTP 404 "Session not found". Invalid JSON and a JSON list fail before
session lookup. keep_count null copies the whole history. The handler does
not call get_session, does not open SessionLocal, and does not roll back.
Success stays {"status": "ok", "id", "name", "kept"}. History read,
truncate, add-message, delete-messages, edit-message, mark-stopped,
update-last-meta, merge, topics, and compact were not changed. Clients
were not changed.
"""
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
os.makedirs("data", exist_ok=True)

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.history_routes as history_routes
from core.database import ChatMessage as DbChatMessage, Session as DbSession, SessionLocal
from core.models import ChatMessage

_FORK_SECRET = "FORK_SECRET session fork token=do-not-return"
_KEY_SECRET = "KEY_SECRET session missing token=do-not-return"
_JSON_SECRET = "JSON_SECRET session body token=do-not-return"
_QUERY_SECRET = "QUERY_SECRET session query token=do-not-return"
_SAVE_SECRET = "SAVE_SECRET session save token=do-not-return"
_FIXED_ERROR = "Failed to fork session"
_SECRETS = (_FORK_SECRET, _KEY_SECRET, _JSON_SECRET, _QUERY_SECRET, _SAVE_SECRET)
_REPO = Path(__file__).resolve().parent.parent
_MODEL = "test-model"
_ENDPOINT = "http://127.0.0.1:9"
_LIST_GET = "'list' object has no attribute 'get'"
_SLICE_TYPE = "slice indices must be integers or None or have an __index__ method"
_LOG = "Session fork failed"
_NEW_ID = "11111111-1111-4111-8111-111111111111"
_DISPATCH = "dispatch blew up"


class _Source:
    def __init__(self, name, endpoint_url, model, history, owner=_FORK_SECRET):
        self.name = name
        self.endpoint_url = endpoint_url
        self.model = model
        self.history = history
        if owner is not _FORK_SECRET:
            self.owner = owner


class _NewSession:
    def __init__(self, fail_at=None, fail_exc=None, http_at=None):
        self.messages = []
        self.fail_at = fail_at
        self.fail_exc = fail_exc
        self.http_at = http_at

    def add_message(self, message):
        n = len(self.messages) + 1
        if self.http_at is not None and n == self.http_at:
            raise HTTPException(status_code=409, detail="Session fork refused")
        if self.fail_at is not None and n == self.fail_at:
            raise self.fail_exc
        self.messages.append(message)


class _Sessions(dict):
    def __init__(self):
        super().__init__()
        self.gets = []
        self.exc = None

    def get(self, key, default=None):
        self.gets.append(key)
        if self.exc is not None:
            raise self.exc
        return super().get(key, default)


class _BoomHistory:
    def __getitem__(self, item):
        raise RuntimeError(_FORK_SECRET)


class _Manager:
    def __init__(self, source=None, source_id=None, create_exc=None, fail_at=None, fail_exc=None, http_at=None):
        self.sessions = _Sessions()
        if source is not None and source_id is not None:
            self.sessions[source_id] = source
        self.create_exc = create_exc
        self.fail_at = fail_at
        self.fail_exc = fail_exc
        self.http_at = http_at
        self.created = []
        self.lookups = []
        self.saves = 0
        self.new_session = None

    def get_session(self, session_id):
        self.lookups.append(session_id)
        raise RuntimeError("get_session should not run")

    def save_sessions(self):
        self.saves += 1
        raise RuntimeError("save_sessions should not run")

    def create_session(self, session_id, name, endpoint_url, model, rag=False, owner=None):
        self.created.append({
            "session_id": session_id,
            "name": name,
            "endpoint_url": endpoint_url,
            "model": model,
            "rag": rag,
            "owner": owner,
        })
        if self.create_exc is not None:
            raise self.create_exc
        new = _NewSession(fail_at=self.fail_at, fail_exc=self.fail_exc, http_at=self.http_at)
        self.new_session = new
        self.sessions[session_id] = new
        return new


@pytest.fixture(autouse=True)
def _guard_history_sessionlocal(monkeypatch):
    state = {"opens": 0}

    def tracking():
        state["opens"] += 1
        raise RuntimeError("history SessionLocal opened")

    monkeypatch.setattr(history_routes, "SessionLocal", tracking)
    yield state
    assert state["opens"] == 0


def _events(monkeypatch, exc=None):
    seen = []

    def record(name, owner=None):
        seen.append((name, owner))
        if exc is not None:
            raise exc

    monkeypatch.setattr("src.event_bus.fire_event", record)
    return seen


def _pin_uuid(monkeypatch, value=_NEW_ID):
    monkeypatch.setattr(history_routes.uuid, "uuid4", lambda: value)
    return value


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
            endpoint_url=_ENDPOINT,
            model=_MODEL,
            owner=owner,
            message_count=message_count,
        ))
        db.commit()
        return session_id
    finally:
        db.close()


def _add_message(session_id, content, when, role="assistant", meta=None):
    db = SessionLocal()
    try:
        msg_id = str(uuid.uuid4())
        db.add(DbChatMessage(
            id=msg_id,
            session_id=session_id,
            role=role,
            content=content,
            meta_data=None if meta is None else json.dumps(meta),
            timestamp=when,
        ))
        db.commit()
        return msg_id
    finally:
        db.close()


def _stored_message(msg_id):
    db = SessionLocal()
    try:
        row = db.query(DbChatMessage).filter(DbChatMessage.id == msg_id).one()
        return row.content, row.meta_data, row.session_id, row.role
    finally:
        db.close()


def _stored_session(session_id):
    db = SessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == session_id).one()
        return row.name, row.endpoint_url, row.model, row.owner, row.message_count
    finally:
        db.close()


def _session_exists(session_id):
    db = SessionLocal()
    try:
        return db.query(DbSession.id).filter(DbSession.id == session_id).first() is not None
    finally:
        db.close()


def _messages_for(session_id):
    db = SessionLocal()
    try:
        return db.query(DbChatMessage.id).filter(DbChatMessage.session_id == session_id).count()
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


def _fork(username, session_id, manager, payload=None, raw=None):
    client = _client(username, manager)
    url = f"/api/session/{session_id}/fork"
    if raw is not None:
        return client.post(url, content=raw, headers={"Content-Type": "application/json"})
    if payload is None:
        payload = {"keep_count": 1}
    return client.post(url, json=payload)


def _assert_no_secrets(resp):
    for secret in _SECRETS:
        assert secret not in resp.text
    assert _LIST_GET not in resp.text
    assert _SLICE_TYPE not in resp.text
    assert _DISPATCH not in resp.text
    assert _LOG not in resp.text


def _assert_fixed(resp):
    assert resp.status_code == 502
    assert resp.json() == {"error": _FIXED_ERROR}
    assert "detail" not in resp.json()
    _assert_no_secrets(resp)


def _assert_manager_idle(manager):
    assert manager.lookups == []
    assert manager.saves == 0


def _memory(role, content, meta=None):
    return ChatMessage(role=role, content=content, metadata=meta)


def _pair():
    user = _memory("user", "hello", {"keep": "yes"})
    assistant = _memory("assistant", "answer", {"variantIndex": 1, "stopped": True})
    return user, assistant


def _source(history, owner):
    return _Source("Project", _ENDPOINT, _MODEL, history, owner=owner)


def _snap(source):
    return (id(source.history), [(id(m), m.role, m.content, None if m.metadata is None else dict(m.metadata)) for m in source.history])


def _assert_source_same(source, snap):
    hist_id, rows = snap
    assert id(source.history) == hist_id
    assert len(source.history) == len(rows)
    for msg, (msg_id, role, content, meta) in zip(source.history, rows):
        assert id(msg) == msg_id
        assert msg.role == role
        assert msg.content == content
        if meta is None:
            assert msg.metadata is None
        else:
            assert msg.metadata == meta


def test_fork_copies_the_prefix_and_leaves_the_database(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=7)
    stored_id = _add_message(
        session_id, "stored-row", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "db"}
    )
    theirs = _add_session(owner, name="Theirs", message_count=1)
    theirs_id = _add_message(
        theirs, "theirs", datetime(2026, 1, 1, 0, 0, 2), meta={"keep": "foreign"}
    )
    legacy = _add_session(None, name="Legacy", message_count=3)
    user, assistant = _pair()
    tail = _memory("user", "later", {"keep": "tail"})
    source = _source([user, assistant, tail], owner)
    snap = _snap(source)
    manager = _Manager(source=source, source_id=session_id)
    new_id = _pin_uuid(monkeypatch)
    events = _events(monkeypatch)
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 1})
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "ok",
            "id": new_id,
            "name": "\u2ADD Project",
            "kept": 1,
        }
        assert "error" not in resp.json()
        assert manager.sessions.gets == [session_id]
        assert manager.created == [{
            "session_id": new_id,
            "name": "\u2ADD Project",
            "endpoint_url": _ENDPOINT,
            "model": _MODEL,
            "rag": False,
            "owner": owner,
        }]
        assert events == [("session_created", owner)]
        assert manager.new_session is not None
        assert len(manager.new_session.messages) == 1
        copied = manager.new_session.messages[0]
        assert copied is not user
        assert isinstance(copied, ChatMessage)
        assert copied.role == "user"
        assert copied.content == "hello"
        assert copied.metadata is user.metadata
        assert copied.metadata == {"keep": "yes"}
        _assert_source_same(source, snap)
        _assert_manager_idle(manager)
        assert _stored_session(session_id) == ("Project", _ENDPOINT, _MODEL, owner, 7)
        content, meta, stored_session, role = _stored_message(stored_id)
        assert stored_session == session_id
        assert role == "assistant"
        assert content == "stored-row"
        assert json.loads(meta) == {"keep": "db"}
        assert _session_exists(new_id) is False
        assert _messages_for(new_id) == 0
        assert _stored_session(theirs) == ("Theirs", _ENDPOINT, _MODEL, owner, 1)
        theirs_content, theirs_meta, _, _ = _stored_message(theirs_id)
        assert theirs_content == "theirs"
        assert json.loads(theirs_meta) == {"keep": "foreign"}
        assert _stored_session(legacy) == ("Legacy", _ENDPOINT, _MODEL, None, 3)
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id, theirs, legacy])


def test_missing_and_zero_keep_count_copy_nothing(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    user, assistant = _pair()
    try:
        for payload in ({}, {"keep_count": 0}):
            source = _source([user, assistant], owner)
            snap = _snap(source)
            manager = _Manager(source=source, source_id=session_id)
            new_id = _pin_uuid(monkeypatch)
            events = _events(monkeypatch)
            resp = _fork(owner, session_id, manager, payload=payload)
            assert resp.status_code == 200
            assert resp.json() == {
                "status": "ok",
                "id": new_id,
                "name": "\u2ADD Project",
                "kept": 0,
            }
            assert manager.created[0]["name"] == "\u2ADD Project"
            assert manager.created[0]["owner"] == owner
            assert manager.new_session.messages == []
            assert events == [("session_created", owner)]
            _assert_source_same(source, snap)
            _assert_manager_idle(manager)
            assert _stored_session(session_id) == ("Project", _ENDPOINT, _MODEL, owner, 2)
            assert _session_exists(new_id) is False
            _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_null_keep_count_copies_the_whole_list(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    user, assistant = _pair()
    source = _source([user, assistant], owner)
    snap = _snap(source)
    manager = _Manager(source=source, source_id=session_id)
    new_id = _pin_uuid(monkeypatch)
    events = _events(monkeypatch)
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": None})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["id"] == new_id
        assert resp.json()["name"] == "\u2ADD Project"
        assert resp.json()["kept"] == 2
        assert len(manager.new_session.messages) == 2
        assert manager.new_session.messages[0].content == "hello"
        assert manager.new_session.messages[0].metadata is user.metadata
        copied_assistant = manager.new_session.messages[1]
        assert copied_assistant is not assistant
        assert copied_assistant.role == "assistant"
        assert copied_assistant.content == "answer"
        assert copied_assistant.metadata is assistant.metadata
        assert copied_assistant.metadata["stopped"] is True
        assert events == [("session_created", owner)]
        _assert_source_same(source, snap)
        _assert_manager_idle(manager)
        assert _stored_session(session_id)[4] == 2
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_keep_count_past_the_end_and_negative_slice(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=3)
    user, assistant = _pair()
    tail = _memory("user", "later", None)
    try:
        source = _source([user, assistant, tail], owner)
        manager = _Manager(source=source, source_id=session_id)
        events = _events(monkeypatch)
        resp = _fork(owner, session_id, manager, payload={"keep_count": 99})
        assert resp.status_code == 200
        assert resp.json()["kept"] == 3
        assert [m.content for m in manager.new_session.messages] == ["hello", "answer", "later"]
        assert manager.new_session.messages[2].metadata is None
        assert events == [("session_created", owner)]
        _assert_manager_idle(manager)

        source = _source([user, assistant, tail], owner)
        snap = _snap(source)
        manager = _Manager(source=source, source_id=session_id)
        events = _events(monkeypatch)
        resp = _fork(owner, session_id, manager, payload={"keep_count": -1})
        assert resp.status_code == 200
        assert resp.json()["kept"] == 2
        assert [m.content for m in manager.new_session.messages] == ["hello", "answer"]
        assert events == [("session_created", owner)]
        _assert_source_same(source, snap)
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_fork_uses_the_source_owner_attribute(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    attributed = owner + "-source"
    user, _assistant = _pair()
    source = _source([user], attributed)
    manager = _Manager(source=source, source_id=session_id)
    events = _events(monkeypatch)
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 1})
        assert resp.status_code == 200
        assert resp.json()["kept"] == 1
        assert manager.created[0]["owner"] == attributed
        assert events == [("session_created", attributed)]
        _assert_manager_idle(manager)
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_missing_source_owner_attribute_passes_none(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=0)
    source = _Source("Project", _ENDPOINT, _MODEL, [])
    assert not hasattr(source, "owner")
    manager = _Manager(source=source, source_id=session_id)
    events = _events(monkeypatch)
    try:
        resp = _fork(owner, session_id, manager, payload={})
        assert resp.status_code == 200
        assert resp.json()["kept"] == 0
        assert resp.json()["name"] == "\u2ADD Project"
        assert manager.created[0]["owner"] is None
        assert events == [("session_created", None)]
        _assert_manager_idle(manager)
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_invalid_json_hides_the_body(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    user, _assistant = _pair()
    source = _source([user], owner)
    snap = _snap(source)
    manager = _Manager(source=source, source_id=session_id)
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    raw = f"{_JSON_SECRET} not-json".encode()
    try:
        resp = _fork(owner, session_id, manager, raw=raw)
        _assert_fixed(resp)
        assert manager.sessions.gets == []
        assert manager.created == []
        assert events == []
        assert manager.new_session is None
        _assert_source_same(source, snap)
        _assert_manager_idle(manager)
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _stored_session(session_id)[4] == 1
    finally:
        _delete_rows([session_id])


def test_json_list_fails_before_lookup(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    source = _source(list(_pair()), owner)
    manager = _Manager(source=source, source_id=session_id)
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    raw = json.dumps([_JSON_SECRET]).encode()
    try:
        resp = _fork(owner, session_id, manager, raw=raw)
        _assert_fixed(resp)
        assert manager.sessions.gets == []
        assert manager.created == []
        assert events == []
        _assert_manager_idle(manager)
        assert _LIST_GET in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_missing_memory_session_stays_404(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=4)
    stored_id = _add_message(
        session_id, "stored-row", datetime(2026, 1, 1, 0, 0, 1), meta={"keep": "db"}
    )
    manager = _Manager()
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 1})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"
        assert "error" not in resp.json()
        assert manager.sessions.gets == [session_id]
        assert manager.created == []
        assert events == []
        _assert_manager_idle(manager)
        content, meta, _, _ = _stored_message(stored_id)
        assert content == "stored-row"
        assert json.loads(meta) == {"keep": "db"}
        assert _stored_session(session_id)[4] == 4
        _assert_no_secrets(resp)
        assert _LOG not in caplog.text
        assert _FIXED_ERROR not in caplog.text
        assert _KEY_SECRET not in caplog.text
    finally:
        _delete_rows([session_id])


def test_keyerror_from_lookup_is_502_not_404(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    source = _source(list(_pair()), owner)
    manager = _Manager(source=source, source_id=session_id)
    manager.sessions.exc = KeyError(_KEY_SECRET)
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 1})
        _assert_fixed(resp)
        assert resp.status_code != 404
        assert "Session not found" not in resp.text
        assert manager.sessions.gets == [session_id]
        assert manager.created == []
        assert events == []
        assert manager.new_session is None
        _assert_manager_idle(manager)
        assert _KEY_SECRET in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _stored_session(session_id) == ("Project", _ENDPOINT, _MODEL, owner, 1)
    finally:
        _delete_rows([session_id])


def test_create_failure_hides_exception(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    user, assistant = _pair()
    source = _source([user, assistant], owner)
    snap = _snap(source)
    manager = _Manager(
        source=source,
        source_id=session_id,
        create_exc=RuntimeError(_SAVE_SECRET),
    )
    new_id = _pin_uuid(monkeypatch)
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 2})
        _assert_fixed(resp)
        assert manager.sessions.gets == [session_id]
        assert manager.created == [{
            "session_id": new_id,
            "name": "\u2ADD Project",
            "endpoint_url": _ENDPOINT,
            "model": _MODEL,
            "rag": False,
            "owner": owner,
        }]
        assert manager.new_session is None
        assert events == []
        _assert_source_same(source, snap)
        _assert_manager_idle(manager)
        assert _session_exists(new_id) is False
        assert _SAVE_SECRET in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _stored_session(session_id)[4] == 2
    finally:
        _delete_rows([session_id])


def test_copy_failure_leaves_the_partial_copy(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    user, assistant = _pair()
    source = _source([user, assistant], owner)
    snap = _snap(source)
    manager = _Manager(
        source=source,
        source_id=session_id,
        fail_at=2,
        fail_exc=RuntimeError(_QUERY_SECRET),
    )
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 2})
        _assert_fixed(resp)
        assert len(manager.created) == 1
        assert manager.new_session is not None
        assert len(manager.new_session.messages) == 1
        assert manager.new_session.messages[0].content == "hello"
        assert manager.new_session.messages[0] is not user
        assert events == []
        _assert_source_same(source, snap)
        _assert_manager_idle(manager)
        assert _QUERY_SECRET in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
        assert _session_exists(manager.created[0]["session_id"]) is False
    finally:
        _delete_rows([session_id])


def test_slice_failure_hides_exception_after_create(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    source = _source([], owner)
    source.history = _BoomHistory()
    manager = _Manager(source=source, source_id=session_id)
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 1})
        _assert_fixed(resp)
        assert len(manager.created) == 1
        assert manager.new_session.messages == []
        assert events == []
        _assert_manager_idle(manager)
        assert _FORK_SECRET in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_empty_string_keep_count_hides_the_type_error(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    user, _assistant = _pair()
    source = _source([user], owner)
    snap = _snap(source)
    manager = _Manager(source=source, source_id=session_id)
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": ""})
        _assert_fixed(resp)
        assert len(manager.created) == 1
        assert manager.new_session.messages == []
        assert events == []
        _assert_source_same(source, snap)
        _assert_manager_idle(manager)
        assert _SLICE_TYPE in caplog.text
        assert f"{_LOG} for {session_id}" in caplog.text
    finally:
        _delete_rows([session_id])


def test_http_exception_from_copy_is_reraised(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=2)
    user, assistant = _pair()
    source = _source([user, assistant], owner)
    snap = _snap(source)
    manager = _Manager(source=source, source_id=session_id, http_at=2)
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 2})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Session fork refused"
        assert "error" not in resp.json()
        assert len(manager.created) == 1
        assert len(manager.new_session.messages) == 1
        assert manager.new_session.messages[0].content == "hello"
        assert events == []
        _assert_source_same(source, snap)
        _assert_manager_idle(manager)
        _assert_no_secrets(resp)
        assert _LOG not in caplog.text
        assert _FIXED_ERROR not in caplog.text
        assert _stored_session(session_id)[4] == 2
    finally:
        _delete_rows([session_id])


def test_event_failure_is_swallowed(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project", message_count=1)
    user, _assistant = _pair()
    source = _source([user], owner)
    snap = _snap(source)
    manager = _Manager(source=source, source_id=session_id)
    new_id = _pin_uuid(monkeypatch)
    events = _events(monkeypatch, exc=RuntimeError(_DISPATCH))
    caplog.set_level(logging.DEBUG, logger="routes.history_routes")
    try:
        resp = _fork(owner, session_id, manager, payload={"keep_count": 1})
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "ok",
            "id": new_id,
            "name": "\u2ADD Project",
            "kept": 1,
        }
        assert events == [("session_created", owner)]
        assert len(manager.new_session.messages) == 1
        _assert_source_same(source, snap)
        _assert_manager_idle(manager)
        _assert_no_secrets(resp)
        assert "session_created event dispatch failed" in caplog.text
        assert _DISPATCH in caplog.text
        assert _LOG not in caplog.text
        assert _FIXED_ERROR not in caplog.text
    finally:
        _delete_rows([session_id])


def test_other_owner_null_owner_and_missing_row_stay_owner_404(caplog, monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    stranger = f"health-{uuid.uuid4().hex}"
    theirs = _add_session(stranger, name="Theirs")
    legacy = _add_session(None, name="Legacy")
    missing = str(uuid.uuid4())
    manager = _Manager()
    events = _events(monkeypatch)
    caplog.set_level(logging.ERROR, logger="routes.history_routes")
    raw = f"{_JSON_SECRET} not-json".encode()
    try:
        other = _fork(owner, theirs, manager, raw=raw)
        blank = _fork(owner, legacy, manager, raw=raw)
        gone = _fork(owner, missing, manager, raw=raw)
        assert other.status_code == 404
        assert other.json()["detail"] == f"Session {theirs} not found"
        assert blank.status_code == 404
        assert blank.json()["detail"] == f"Session {legacy} not found"
        assert gone.status_code == 404
        assert gone.json()["detail"] == f"Session {missing} not found"
        assert "error" not in other.json()
        assert "error" not in blank.json()
        assert "error" not in gone.json()
        assert manager.sessions.gets == []
        assert manager.created == []
        assert events == []
        _assert_manager_idle(manager)
        _assert_no_secrets(other)
        _assert_no_secrets(blank)
        _assert_no_secrets(gone)
        assert _JSON_SECRET not in caplog.text
        assert _LOG not in caplog.text
        assert _stored_session(theirs)[0] == "Theirs"
        assert _stored_session(legacy) == ("Legacy", _ENDPOINT, _MODEL, None, 0)
    finally:
        _delete_rows([theirs, legacy])


def test_anonymous_is_403(monkeypatch):
    owner = f"health-{uuid.uuid4().hex}"
    session_id = _add_session(owner, name="Project")
    manager = _Manager(source=_source([], owner), source_id=session_id)
    events = _events(monkeypatch)
    try:
        resp = _fork(None, session_id, manager, raw=f"{_JSON_SECRET} not-json".encode())
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Authentication required"
        assert "error" not in resp.json()
        assert manager.sessions.gets == []
        assert manager.created == []
        assert events == []
        _assert_manager_idle(manager)
        _assert_no_secrets(resp)
    finally:
        _delete_rows([session_id])


def test_fork_keeps_the_fixed_sentence_and_leaves_neighbors():
    history = (_REPO / "routes/history_routes.py").read_text(encoding="utf-8")
    assert "def history_error(message: str, status_code: int = 502, **extra):" in history
    assert history.count("def history_error(") == 1
    assert "from fastapi.responses import JSONResponse" in history
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
    compact_start = history.index("    async def compact_session(")
    loaded = history[history_start:truncate_start]
    truncate = history[truncate_start:add_start]
    added = history[add_start:delete_start]
    deleted = history[delete_start:edit_start]
    edited = history[edit_start:mark_start]
    marked = history[mark_start:update_start]
    updated = history[update_start:merge_start]
    merged = history[merge_start:fork_start]
    forked = history[fork_start:topics_start]
    topics = history[topics_start:compact_start]
    compact = history[compact_start:]

    assert "history_error" not in loaded
    assert 'logger.error(f"DB fallback failed for {session_id}: {e}")' in loaded

    assert "history_error" not in truncate
    assert "except HTTPException:" not in truncate
    assert 'logger.error(f"Truncate error {session_id}: {e}")' in truncate
    assert "raise HTTPException(500, str(e))" in truncate
    assert 'body.get("keep_count", 0)' in truncate

    assert "history_error" not in added
    assert "except HTTPException:" not in added
    assert 'raise HTTPException(400, "content is required")' in added
    assert "except KeyError:" in added

    assert "history_error" not in deleted
    assert "except HTTPException:" not in deleted
    assert 'logger.error(f"Delete messages error {session_id}: {e}")' in deleted
    assert "raise HTTPException(500, str(e))" in deleted

    assert "history_error" not in edited
    assert "except HTTPException:" in edited
    assert 'logger.error(f"Edit message error {session_id}: {e}")' in edited
    assert "raise HTTPException(500, str(e))" in edited

    assert "history_error" not in marked
    assert "except HTTPException:" not in marked
    assert 'logger.error(f"Mark stopped error {session_id}: {e}")' in marked
    assert "raise HTTPException(500, str(e))" in marked

    assert "history_error" not in updated
    assert "except HTTPException:" not in updated
    assert 'logger.error(f"Update last meta error {session_id}: {e}")' in updated
    assert "raise HTTPException(500, str(e))" in updated
    assert 'body.get("metadata", {})' in updated

    assert "history_error" not in merged
    assert "except HTTPException:" not in merged
    assert 'logger.error(f"Merge assistant error {session_id}: {e}")' in merged
    assert "raise HTTPException(500, str(e))" in merged
    assert 'return {"status": "ok", "merged": False}' in merged
    assert 'return {"status": "ok", "merged": True}' in merged
    assert "request.json()" in merged
    assert 'body.get("separator", "\\n\\n")' in merged
    assert "db.rollback()" not in merged
    assert merged.index("request.json()") < merged.index("get_session(")

    owner_at = forked.index("_verify_session_owner(request, session_id)")
    try_at = forked.index("try:")
    assert owner_at < try_at
    assert forked.index("request.json()") < forked.index('body.get("keep_count", 0)')
    assert forked.index('body.get("keep_count", 0)') < forked.index("sessions.get(session_id)")
    assert forked.index("sessions.get(session_id)") < forked.index('raise HTTPException(404, "Session not found")')
    assert forked.index('raise HTTPException(404, "Session not found")') < forked.index("create_session(")
    assert forked.index("create_session(") < forked.index("source.history[:keep_count]")
    assert forked.index("source.history[:keep_count]") < forked.index("add_message(")
    assert forked.index("add_message(") < forked.index('fire_event("session_created"')
    assert forked.index('fire_event("session_created"') < forked.index('"status": "ok"')
    assert 'fork_name = f"\\u2ADD {source.name}"' in forked
    assert "ChatMessage(msg.role, msg.content, msg.metadata)" in forked
    assert "getattr(source, 'owner', None)" in forked
    assert "rag=False" in forked
    assert 'logger.debug("session_created event dispatch failed", exc_info=True)' in forked
    assert 'history_error("Failed to fork session")' in forked
    assert 'logger.exception("Session fork failed for %s", session_id)' in forked
    assert '"id": new_id' in forked
    assert '"name": fork_name' in forked
    assert '"kept": len(msgs_to_copy)' in forked
    assert "get_session(" not in forked
    assert "save_sessions" not in forked
    assert "SessionLocal" not in forked
    assert "db.rollback()" not in forked
    assert "except KeyError:" not in forked
    assert "{e}" not in forked
    assert "str(e)" not in forked
    assert "HTTPException(500" not in forked
    assert 'logger.error(f"Fork error {session_id}: {e}")' not in forked
    assert forked.count("except HTTPException:") == 1
    assert forked.count("except Exception:") == 2
    http_at = forked.index("except HTTPException:")
    assert forked.index("except Exception:") < http_at
    assert forked.index("except Exception:", http_at) > http_at
    assert forked.index('history_error("Failed to fork session")') > http_at
    assert history.count('history_error("Failed to fork session")') == 1
    assert 'history_error("Failed to merge assistant messages")' not in history
    assert 'history_error("Failed to update message metadata")' not in history
    assert 'history_error("Failed to mark message stopped")' not in history

    assert "history_error" not in topics
    assert "except HTTPException:" not in topics
    assert 'raise HTTPException(500, f"Topic analysis failed: {e}")' in topics
    assert "require_user(request)" in topics
    assert "analyze_topics(session_manager, owner=user or None)" in topics

    assert "history_error" not in compact
    assert "except HTTPException:" not in compact
    assert 'logger.error(f"Manual compact error {session_id}: {e}")' in compact
    assert "raise HTTPException(500, str(e))" in compact
    assert 'HTTPException(404, "Session not found")' in compact

    chat = (_REPO / "static/js/chat.js").read_text(encoding="utf-8")
    assert chat.count("merge-last-assistant failed:") == 1
    assert chat.count("update-last-meta (variants) failed:") == 1
    assert chat.count("update-last-meta (variantIndex) failed:") == 1
    assert chat.count("update-last-meta") == 5
    assert chat.count("mark-stopped failed:") == 2
    fork_fn = chat.index("  export async function forkFrom(")
    fork_end = chat.index("\n  export async function ", fork_fn + 10)
    fork_client = chat[fork_fn:fork_end]
    assert "api/session/${sessionId}/fork" in fork_client
    assert "await res.text()" in fork_client
    assert "res.ok" in fork_client
    assert "const data = await res.json()" in fork_client
    assert "Fork failed: " in fork_client
    assert "keep_count: keepCount" in fork_client
    assert "history_error" not in fork_client

    slash = (_REPO / "static/js/slashCommands.js").read_text(encoding="utf-8")
    slash_fn = slash.index("async function _cmdSessionFork(")
    slash_end = slash.index("async function _cmdSessionTruncate(")
    slash_fork = slash[slash_fn:slash_end]
    assert "api/session/${ctx.sid}/fork" in slash_fork
    assert "slashReply('Fork failed')" in slash_fork
    assert "keep_count: keepCount" in slash_fork
    assert ".text(" not in slash_fork
    assert "history_error" not in slash_fork

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
