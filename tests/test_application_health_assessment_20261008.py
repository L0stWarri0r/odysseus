"""Gallery AI upscale and style transfer must not echo exception text."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.gallery_routes as gallery_routes


SECRET = "super-secret-image-key"
ENDPOINT = "http://10.1.2.3:7860/v1"
PNG = ("photo.png", b"\x89PNG\r\n\x1a\n", "image/png")


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _DB:
    def __init__(self, row):
        self._row = row

    def query(self, _model):
        return _Query(self._row)

    def close(self):
        return None


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, json=None):
        self.posts.append(url)
        if self._error is not None:
            raise self._error
        return self._response


def _mount(monkeypatch, *, endpoint=ENDPOINT, client=None):
    row = None
    if endpoint is not None:
        row = SimpleNamespace(base_url=endpoint, model_type="image", is_enabled=True)
    monkeypatch.setattr(gallery_routes, "SessionLocal", lambda: _DB(row))
    monkeypatch.setattr(gallery_routes, "require_privilege", lambda _request, _key: "tester")

    class _Factory:
        def __init__(self, *_args, **_kwargs):
            self._inner = client

        async def __aenter__(self):
            if self._inner is None:
                raise AssertionError("image endpoint client should not be opened")
            return await self._inner.__aenter__()

        async def __aexit__(self, *args):
            return await self._inner.__aexit__(*args)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Factory)
    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def _leak():
    return RuntimeError(f"ConnectError: {ENDPOINT}?api_key={SECRET}")


def test_ai_upscale_exception_is_502_without_exception_text(monkeypatch):
    client = _mount(monkeypatch, client=_Client(error=_leak()))
    res = client.post("/api/gallery/ai-upscale", files={"image": PNG}, data={"scale": "2"})
    assert res.status_code == 502
    assert res.json() == {"error": "Upscale failed"}
    assert SECRET not in res.text
    assert "10.1.2.3" not in res.text


def test_style_transfer_exception_is_502_without_exception_text(monkeypatch):
    client = _mount(monkeypatch, client=_Client(error=_leak()))
    res = client.post(
        "/api/gallery/style-transfer",
        files={"image": PNG},
        data={"prompt": "oil painting", "strength": "0.4"},
    )
    assert res.status_code == 502
    assert res.json() == {"error": "Style transfer failed"}
    assert SECRET not in res.text
    assert "10.1.2.3" not in res.text


def test_ai_upscale_bad_json_does_not_return_body(monkeypatch):
    upstream = _Client(response=_Response(200, payload=ValueError(f"invalid json {SECRET}")))
    client = _mount(monkeypatch, client=upstream)
    res = client.post("/api/gallery/ai-upscale", files={"image": PNG})
    assert res.status_code == 502
    assert res.json() == {"error": "Upscale failed"}
    assert SECRET not in res.text
    assert upstream.posts == [f"{ENDPOINT}/images/upscale"]


def test_style_transfer_bad_json_does_not_return_body(monkeypatch):
    upstream = _Client(response=_Response(200, payload=ValueError(f"invalid json {SECRET}")))
    client = _mount(monkeypatch, client=upstream)
    res = client.post("/api/gallery/style-transfer", files={"image": PNG}, data={"prompt": "ink"})
    assert res.status_code == 502
    assert res.json() == {"error": "Style transfer failed"}
    assert SECRET not in res.text
    assert upstream.posts == [f"{ENDPOINT}/images/generations"]


def test_ai_upscale_success_stays_200(monkeypatch):
    upstream = _Client(response=_Response(200, payload={"data": [{"b64_json": "abc123"}]}))
    client = _mount(monkeypatch, client=upstream)
    res = client.post("/api/gallery/ai-upscale", files={"image": PNG}, data={"scale": "4"})
    assert res.status_code == 200
    assert res.json() == {"image": "abc123"}
    assert "error" not in res.json()


def test_style_transfer_success_stays_200(monkeypatch):
    upstream = _Client(response=_Response(200, payload={"data": [{"b64_json": "styled"}]}))
    client = _mount(monkeypatch, client=upstream)
    res = client.post(
        "/api/gallery/style-transfer",
        files={"image": PNG},
        data={"prompt": "watercolor", "strength": "0.55"},
    )
    assert res.status_code == 200
    assert res.json() == {"image": "styled"}


def test_upstream_status_stays_fixed_sentence(monkeypatch):
    body = f"upstream blew up token={SECRET}"
    upscale = _mount(monkeypatch, client=_Client(response=_Response(503, text=body)))
    up_res = upscale.post("/api/gallery/ai-upscale", files={"image": PNG})
    assert up_res.status_code == 200
    assert up_res.json() == {"error": "Upscale endpoint not available (503)"}
    assert SECRET not in up_res.text

    style = _mount(monkeypatch, client=_Client(response=_Response(404, text=body)))
    style_res = style.post("/api/gallery/style-transfer", files={"image": PNG}, data={"prompt": "ink"})
    assert style_res.status_code == 200
    assert style_res.json() == {"error": "Style transfer failed (404)"}
    assert SECRET not in style_res.text


def test_missing_image_is_400_and_does_not_call_upstream(monkeypatch):
    client = _mount(monkeypatch, client=None)
    up = client.post("/api/gallery/ai-upscale", data={"scale": "2"})
    assert up.status_code == 400
    style = client.post("/api/gallery/style-transfer", data={"prompt": "ink"})
    assert style.status_code == 400


def test_missing_endpoint_is_400_and_does_not_call_upstream(monkeypatch):
    client = _mount(monkeypatch, endpoint=None, client=None)
    up = client.post("/api/gallery/ai-upscale", files={"image": PNG})
    assert up.status_code == 400
    assert "No image generation endpoint" in up.json()["detail"]
    style = client.post("/api/gallery/style-transfer", files={"image": PNG}, data={"prompt": "ink"})
    assert style.status_code == 400
    assert style.json()["detail"] == "No image generation endpoint configured."
