"""Gallery AI tagging must not echo vision-model bodies or exception text."""

import os
import uuid
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
Path("data").mkdir(exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.gallery_routes as gallery_routes
from core.database import GalleryImage, SessionLocal

SECRET = "sk-vision-super-secret"
ENDPOINT = f"http://127.0.0.1:9/v1/chat/completions?key={SECRET}"
UPSTREAM_BODY = f"invalid api key {SECRET} at internal-vision.local"
OWNER = "health-20261010"
DISABLED = "Vision is disabled — enable it in Settings → Vision"
NO_MODEL = "No vision model configured — set one in Settings → Vision"
NO_ENDPOINT = "No vision-capable endpoint configured"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(gallery_routes, "get_current_user", lambda _request: OWNER)
    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def _seed(filename: str, *, with_file: bool) -> str:
    image_id = uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(GalleryImage(
            id=image_id,
            filename=filename,
            owner=OWNER,
            prompt="",
            ai_tags="",
        ))
        db.commit()
    finally:
        db.close()
    if with_file:
        dest = Path("data/generated_images")
        dest.mkdir(parents=True, exist_ok=True)
        (dest / filename).write_bytes(b"\x89PNG\r\n")
    return image_id


def _cleanup(image_id: str, filename: str) -> None:
    db = SessionLocal()
    try:
        db.query(GalleryImage).filter(GalleryImage.id == image_id).delete()
        db.commit()
    finally:
        db.close()
    path = Path("data/generated_images") / filename
    if path.exists():
        path.unlink()


def _tags(image_id: str):
    db = SessionLocal()
    try:
        row = db.query(GalleryImage).filter(GalleryImage.id == image_id).one()
        return row.ai_tags
    finally:
        db.close()


class _Resp:
    def __init__(self, status_code=200, text="", payload=None, json_error=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise ValueError(self._json_error)
        return self._payload


def _http_client(response=None, error=None):
    class Client:
        calls = 0

        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            type(self).calls += 1
            if error is not None:
                raise error
            return response

    return Client


def _patch_vision(monkeypatch, *, settings=None, resolved=None, resolve_error=None, client=None):
    import httpx
    import src.document_processor as document_processor

    state = {"resolve_calls": 0, "settings_calls": 0}

    def load_settings():
        state["settings_calls"] += 1
        if settings is None:
            return {"vision_enabled": True, "vision_model": "llava"}
        return settings

    def resolve(_configured):
        state["resolve_calls"] += 1
        if resolve_error is not None:
            raise resolve_error
        if resolved is None:
            return (ENDPOINT, "llava", {"Authorization": f"Bearer {SECRET}"})
        return resolved

    monkeypatch.setattr(document_processor, "_load_vl_settings", load_settings)
    monkeypatch.setattr(document_processor, "_resolve_vl_model", resolve)
    if client is not None:
        monkeypatch.setattr(httpx, "AsyncClient", client)
    return state


def _post(monkeypatch, image_id: str):
    return _client(monkeypatch).post(f"/api/gallery/{image_id}/ai-tag")


def test_upstream_error_hides_body(monkeypatch):
    filename = f"{uuid.uuid4().hex}.png"
    image_id = _seed(filename, with_file=True)
    client = _http_client(_Resp(status_code=401, text=UPSTREAM_BODY))
    try:
        _patch_vision(monkeypatch, client=client)
        res = _post(monkeypatch, image_id)
        assert res.status_code == 200
        assert res.json() == {"error": "Vision model returned 401"}
        assert SECRET not in res.text
        assert "internal-vision.local" not in res.text
        assert "invalid api key" not in res.text
        assert client.calls == 1
        assert _tags(image_id) in ("", None)
    finally:
        _cleanup(image_id, filename)


def test_exception_is_502_without_exception_text(monkeypatch):
    filename = f"{uuid.uuid4().hex}.png"
    image_id = _seed(filename, with_file=True)
    client = _http_client(error=RuntimeError(f"connect failed for {ENDPOINT}"))
    try:
        _patch_vision(monkeypatch, client=client)
        res = _post(monkeypatch, image_id)
        assert res.status_code == 502
        assert res.json() == {"error": "AI tagging failed"}
        assert SECRET not in res.text
        assert ENDPOINT not in res.text
        assert "connect failed" not in res.text
        assert _tags(image_id) in ("", None)
    finally:
        _cleanup(image_id, filename)


def test_non_json_success_body_is_502_without_body(monkeypatch):
    filename = f"{uuid.uuid4().hex}.png"
    image_id = _seed(filename, with_file=True)
    client = _http_client(_Resp(
        status_code=200,
        text=UPSTREAM_BODY,
        json_error=f"Expecting value: {UPSTREAM_BODY}",
    ))
    try:
        _patch_vision(monkeypatch, client=client)
        res = _post(monkeypatch, image_id)
        assert res.status_code == 502
        assert res.json() == {"error": "AI tagging failed"}
        assert SECRET not in res.text
        assert "internal-vision.local" not in res.text
        assert _tags(image_id) in ("", None)
    finally:
        _cleanup(image_id, filename)


def test_success_stays_200_and_stores_tags(monkeypatch):
    filename = f"{uuid.uuid4().hex}.png"
    image_id = _seed(filename, with_file=True)
    payload = {
        "choices": [{"message": {"content": "Cat, Window Light, Extra"}}],
        "debug": {"api_key": SECRET, "url": ENDPOINT},
    }
    client = _http_client(_Resp(status_code=200, text="ok", payload=payload))
    try:
        _patch_vision(monkeypatch, client=client)
        res = _post(monkeypatch, image_id)
        assert res.status_code == 200
        assert res.json() == {"ok": True, "ai_tags": "cat, window light, extra"}
        assert SECRET not in res.text
        assert ENDPOINT not in res.text
        assert _tags(image_id) == "cat, window light, extra"
    finally:
        _cleanup(image_id, filename)


def test_vision_disabled_stays_fixed_sentence_and_does_not_call(monkeypatch):
    filename = f"{uuid.uuid4().hex}.png"
    image_id = _seed(filename, with_file=True)
    client = _http_client(_Resp(status_code=500, text=UPSTREAM_BODY))
    try:
        state = _patch_vision(
            monkeypatch,
            settings={"vision_enabled": False, "vision_model": "llava"},
            client=client,
        )
        res = _post(monkeypatch, image_id)
        assert res.status_code == 200
        assert res.json() == {"error": DISABLED}
        assert client.calls == 0
        assert state["resolve_calls"] == 0
        assert SECRET not in res.text
    finally:
        _cleanup(image_id, filename)


def test_missing_model_stays_fixed_sentence_and_does_not_call(monkeypatch):
    filename = f"{uuid.uuid4().hex}.png"
    image_id = _seed(filename, with_file=True)
    client = _http_client(_Resp(status_code=500, text=UPSTREAM_BODY))
    try:
        _patch_vision(
            monkeypatch,
            resolve_error=ValueError(f"no model token={SECRET}"),
            client=client,
        )
        res = _post(monkeypatch, image_id)
        assert res.status_code == 200
        assert res.json() == {"error": NO_MODEL}
        assert client.calls == 0
        assert SECRET not in res.text
        assert "no model token" not in res.text
    finally:
        _cleanup(image_id, filename)


def test_empty_endpoint_stays_fixed_sentence_and_does_not_call(monkeypatch):
    filename = f"{uuid.uuid4().hex}.png"
    image_id = _seed(filename, with_file=True)
    client = _http_client(_Resp(status_code=500, text=UPSTREAM_BODY))
    try:
        _patch_vision(monkeypatch, resolved=("", "llava", None), client=client)
        res = _post(monkeypatch, image_id)
        assert res.status_code == 200
        assert res.json() == {"error": NO_ENDPOINT}
        assert client.calls == 0
        assert SECRET not in res.text
    finally:
        _cleanup(image_id, filename)


def test_missing_file_is_404_and_does_not_resolve(monkeypatch):
    filename = f"{uuid.uuid4().hex}.png"
    image_id = _seed(filename, with_file=False)
    client = _http_client(error=RuntimeError(f"should not post {SECRET}"))
    try:
        state = _patch_vision(monkeypatch, client=client)
        res = _post(monkeypatch, image_id)
        assert res.status_code == 404
        assert res.json()["detail"] == "Image file not found"
        assert client.calls == 0
        assert state["resolve_calls"] == 0
        assert state["settings_calls"] == 0
        assert SECRET not in res.text
    finally:
        _cleanup(image_id, filename)


def test_unknown_image_is_404_and_does_not_resolve(monkeypatch):
    client = _http_client(error=RuntimeError(SECRET))
    state = _patch_vision(monkeypatch, client=client)
    res = _post(monkeypatch, uuid.uuid4().hex)
    assert res.status_code == 404
    assert res.json()["detail"] == "Image not found"
    assert client.calls == 0
    assert state["resolve_calls"] == 0
    assert SECRET not in res.text
