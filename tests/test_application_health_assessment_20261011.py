"""Face enhancement must not echo GFPGAN exception text."""

import base64
import io
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/app.db")
Path("data").mkdir(exist_ok=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import routes.gallery_routes as gallery_routes


SECRET = "super-secret-gfpgan-token"
MODEL_URL = (
    "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth"
)
WEIGHT_ROOT = "/home/odysseus/.cache/gfpgan_models"


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


class _FailingRestorer:
    def __init__(self, **kwargs):
        path = kwargs.get("model_path") or MODEL_URL
        root = kwargs.get("model_rootpath") or WEIGHT_ROOT
        raise RuntimeError(f"weights failed at {path} root={root} token={SECRET}")

    def enhance(self, img_bgr, has_aligned=False, only_center_face=False, paste_back=True):
        raise AssertionError("enhance should not run after a constructor failure")


class _WorkingRestorer:
    def __init__(self, **_kwargs):
        return None

    def enhance(self, img_bgr, has_aligned=False, only_center_face=False, paste_back=True):
        return None, None, img_bgr


def _install_cv2(monkeypatch):
    cv2 = _module("cv2")
    cv2.COLOR_RGB2BGR = 1
    cv2.COLOR_BGR2RGB = 2

    def cvtColor(arr, _code):
        return arr

    cv2.cvtColor = cvtColor
    monkeypatch.setitem(sys.modules, "cv2", cv2)


def _install_gfpgan(monkeypatch, restorer):
    gfpgan = _module("gfpgan")
    gfpgan.GFPGANer = restorer
    monkeypatch.setitem(sys.modules, "gfpgan", gfpgan)
    _install_cv2(monkeypatch)


class _ImportBoom(types.ModuleType):
    def __getattr__(self, name):
        raise ImportError(f"cannot import {name} token={SECRET} url={MODEL_URL}")


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(gallery_routes, "require_privilege", lambda _request, _key: "tester")
    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def _assert_no_leak(text: str):
    assert SECRET not in text
    assert MODEL_URL not in text
    assert "github.com" not in text
    assert WEIGHT_ROOT not in text
    assert "gfpgan_models" not in text
    assert "weights failed" not in text
    assert "cannot import" not in text


def test_enhance_face_exception_is_502_without_exception_text(monkeypatch):
    _install_gfpgan(monkeypatch, _FailingRestorer)
    res = _client(monkeypatch).post("/api/image/enhance-face", json={"image": _png_b64()})
    assert res.status_code == 502
    assert res.json() == {"error": "Face enhancement failed"}
    _assert_no_leak(res.text)


def test_gfpgan_import_error_stays_pil_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "gfpgan", _ImportBoom("gfpgan"))
    res = _client(monkeypatch).post("/api/image/enhance-face", json={"image": _png_b64()})
    assert res.status_code == 200
    body = res.json()
    assert body["method"] == "pil"
    assert "error" not in body
    Image.open(io.BytesIO(base64.b64decode(body["image"])))
    _assert_no_leak(res.text)


def test_success_stays_200_image(monkeypatch):
    _install_gfpgan(monkeypatch, _WorkingRestorer)
    res = _client(monkeypatch).post("/api/image/enhance-face", json={"image": _png_b64()})
    assert res.status_code == 200
    body = res.json()
    assert "error" not in body
    assert "method" not in body
    Image.open(io.BytesIO(base64.b64decode(body["image"])))
    _assert_no_leak(res.text)


def test_missing_image_is_400_and_does_not_load_model(monkeypatch):
    calls = {"n": 0}

    class _CountingRestorer:
        def __init__(self, **_kwargs):
            calls["n"] += 1
            raise RuntimeError(f"model loaded token={SECRET} url={MODEL_URL}")

    _install_gfpgan(monkeypatch, _CountingRestorer)
    client = _client(monkeypatch)
    missing = client.post("/api/image/enhance-face", json={})
    empty = client.post("/api/image/enhance-face", json={"image": ""})
    assert missing.status_code == 400
    assert empty.status_code == 400
    assert missing.json()["detail"] == "No image provided"
    assert empty.json()["detail"] == "No image provided"
    assert calls["n"] == 0
    _assert_no_leak(missing.text)
    _assert_no_leak(empty.text)
