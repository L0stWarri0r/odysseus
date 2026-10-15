"""Deleting a gallery image must not return database exception text.

DELETE /api/gallery/{image_id} used to raise HTTP 500 with str(e). A failed
commit or rollback can include the statement and the filename being removed.
Unexpected failures, including a rollback failure after the original error,
now return HTTP 502 with a fixed sentence. The exception is logged and is not
returned. Success stays HTTP 200 with the existing deleted payload and no
error key. Chat-history cleanup stays best-effort: a cleanup failure is
logged and the delete still succeeds. A missing image, another owner's image,
and an anonymous caller stay HTTP 404.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timedelta

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as SASession

import routes.gallery_routes as gallery_routes
from core.database import ChatMessage, GalleryImage, Session as DbSession

_COMMIT_SECRET = "COMMIT_SECRET unique constraint gallery_images.filename=/data/app.db"
_ROLLBACK_SECRET = "ROLLBACK_SECRET could not roll back /data/app.db"
_CLEANUP_SECRET = "CLEANUP_SECRET chat metadata image prompt=/data/app.db"
_IMG_DIR = os.path.join("data", "generated_images")
# Saved before any test patches SessionLocal. Reads and cleanup must use this.
_REAL_SESSION = gallery_routes.SessionLocal


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def _real_session():
    return _REAL_SESSION()


def _add(owner, prompt=""):
    db = _real_session()
    try:
        image_id = uuid.uuid4().hex
        filename = f"health-{image_id}.png"
        db.add(GalleryImage(
            id=image_id,
            filename=filename,
            prompt=prompt,
            owner=owner,
            tags="",
            ai_tags="",
            favorite=False,
            is_active=True,
        ))
        db.commit()
        return image_id, filename
    finally:
        db.close()


def _write_file(filename):
    os.makedirs(_IMG_DIR, exist_ok=True)
    path = os.path.join(_IMG_DIR, filename)
    with open(path, "wb") as fh:
        fh.write(b"health-image")
    return path


def _read(image_id):
    db = _real_session()
    try:
        img = db.query(GalleryImage).filter(GalleryImage.id == image_id).one()
        return img.is_active, img.filename, img.owner, img.prompt
    finally:
        db.close()


def _add_session(owner):
    db = _real_session()
    try:
        session_id = uuid.uuid4().hex
        db.add(DbSession(
            id=session_id,
            name="health",
            endpoint_url="http://127.0.0.1:9",
            model="health",
            owner=owner,
        ))
        db.commit()
        return session_id
    finally:
        db.close()


def _add_message(session_id, role, content, meta, when):
    db = _real_session()
    try:
        message_id = uuid.uuid4().hex
        db.add(ChatMessage(
            id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            meta_data=json.dumps(meta) if meta is not None else None,
            timestamp=when,
        ))
        db.commit()
        return message_id
    finally:
        db.close()


def _messages(session_id):
    db = _real_session()
    try:
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.timestamp.asc())
            .all()
        )
        return [(row.role, row.content, row.meta_data) for row in rows]
    finally:
        db.close()


def _delete(ids, session_ids=(), filenames=()):
    db = _real_session()
    try:
        if session_ids:
            db.query(ChatMessage).filter(ChatMessage.session_id.in_(session_ids)).delete(synchronize_session=False)
            db.query(DbSession).filter(DbSession.id.in_(session_ids)).delete(synchronize_session=False)
        if ids:
            db.query(GalleryImage).filter(GalleryImage.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    for filename in filenames:
        path = os.path.join(_IMG_DIR, filename)
        if os.path.exists(path):
            os.remove(path)


def _install_commit_failure(monkeypatch):
    """SessionLocal whose commit and rollback raise, after a real rollback."""
    real_factory = _REAL_SESSION

    def _factory():
        real = real_factory()

        class _Wrap:
            def commit(self):
                raise RuntimeError(_COMMIT_SECRET)

            def rollback(self):
                real.rollback()
                raise RuntimeError(_ROLLBACK_SECRET)

            def close(self):
                real.close()

            def __getattr__(self, name):
                return getattr(real, name)

        return _Wrap()

    monkeypatch.setattr(gallery_routes, "SessionLocal", _factory)


def _install_chat_query_failure(monkeypatch):
    real_query = SASession.query

    def _query(self, *entities, **kwargs):
        for ent in entities:
            if getattr(ent, "__name__", None) == "ChatMessage":
                raise RuntimeError(_CLEANUP_SECRET)
        return real_query(self, *entities, **kwargs)

    monkeypatch.setattr(SASession, "query", _query)


def test_delete_commit_failure_hides_exception(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    private = "leaked-delete-prompt"
    image_id, filename = _add(owner, prompt=private)
    path = _write_file(filename)
    caplog.set_level(logging.ERROR, logger="routes.gallery_routes")
    _install_commit_failure(monkeypatch)
    try:
        resp = _client(owner).delete(f"/api/gallery/{image_id}")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Gallery delete failed"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert private not in resp.text
        assert filename not in resp.text
        assert image_id not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert _read(image_id) == (True, filename, owner, private)
        # The file is removed before the soft-delete commit. A failed commit
        # therefore leaves an active row whose file is already gone.
        assert not os.path.exists(path)
    finally:
        _delete([image_id], filenames=(filename,))


def test_delete_success_removes_file_and_chat_reference():
    owner = f"health-{uuid.uuid4().hex}"
    private = "private-image-prompt"
    image_id, filename = _add(owner, prompt=private)
    path = _write_file(filename)
    session_id = _add_session(owner)
    base = datetime(2026, 10, 15, 12, 0, 0)
    only_event = {"tool_events": [{"image_id": image_id, "image_url": f"/api/generated-image/{filename}"}]}
    mixed_event = {"tool_events": [
        {"image_id": image_id, "image_url": f"/api/generated-image/{filename}"},
        {"image_id": "keep-other", "image_url": "/api/generated-image/keep.png", "name": "keep-me"},
    ]}
    _add_message(session_id, "user", private, None, base)
    _add_message(session_id, "assistant", "Generated image for: secret", only_event, base + timedelta(seconds=1))
    _add_message(session_id, "user", "unrelated prompt", None, base + timedelta(seconds=2))
    _add_message(session_id, "assistant", "still here", mixed_event, base + timedelta(seconds=3))
    try:
        resp = _client(owner).delete(f"/api/gallery/{image_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "deleted", "id": image_id}
        assert "error" not in body
        assert private not in resp.text
        assert not os.path.exists(path)
        assert _read(image_id)[0] is False
        remaining = _messages(session_id)
        assert [(role, content) for role, content, _meta in remaining] == [
            ("user", "unrelated prompt"),
            ("assistant", "still here"),
        ]
        kept_meta = json.loads(remaining[1][2])
        assert kept_meta["tool_events"] == [
            {"image_id": "keep-other", "image_url": "/api/generated-image/keep.png", "name": "keep-me"},
        ]
        assert image_id not in remaining[1][2]
        assert filename not in remaining[1][2]
    finally:
        _delete([image_id], session_ids=(session_id,), filenames=(filename,))


def test_delete_chat_cleanup_failure_still_succeeds(monkeypatch, caplog):
    owner = f"health-{uuid.uuid4().hex}"
    image_id, filename = _add(owner, prompt="kept-out-of-body")
    path = _write_file(filename)
    caplog.set_level(logging.WARNING, logger="routes.gallery_routes")
    _install_chat_query_failure(monkeypatch)
    try:
        resp = _client(owner).delete(f"/api/gallery/{image_id}")
        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "id": image_id}
        assert "error" not in resp.json()
        assert _CLEANUP_SECRET not in resp.text
        assert _CLEANUP_SECRET in caplog.text
        assert not os.path.exists(path)
        assert _read(image_id)[0] is False
    finally:
        _delete([image_id], filenames=(filename,))


def test_delete_other_owner_and_missing_stay_404():
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    image_id, filename = _add(other, prompt="theirs")
    path = _write_file(filename)
    try:
        resp = _client(owner).delete(f"/api/gallery/{image_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Image not found"
        assert "error" not in resp.json()
        assert "Gallery delete failed" not in resp.text
        assert os.path.exists(path)
        assert _read(image_id) == (True, filename, other, "theirs")

        missing = _client(owner).delete(f"/api/gallery/{uuid.uuid4().hex}")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Image not found"
        assert "Gallery delete failed" not in missing.text
    finally:
        _delete([image_id], filenames=(filename,))


def test_delete_anonymous_stays_404():
    owner = f"health-{uuid.uuid4().hex}"
    image_id, filename = _add(owner, prompt="keep-me")
    path = _write_file(filename)
    try:
        resp = _client(None).delete(f"/api/gallery/{image_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Image not found"
        assert "error" not in resp.json()
        assert os.path.exists(path)
        assert _read(image_id) == (True, filename, owner, "keep-me")
    finally:
        _delete([image_id], filenames=(filename,))
