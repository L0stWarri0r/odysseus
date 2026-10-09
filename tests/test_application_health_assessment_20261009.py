"""Local denoise and upscale must not echo Real-ESRGAN exception text."""

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


SECRET = "super-secret-weight-token"
WEIGHT_PATH = "/home/odysseus/.cache/realesrgan/RealESRGAN_x4plus.pth"
INSTALL_MSG = (
    "realesrgan not installed. Install it from Cookbook → Dependencies "
    "(search 'realesrgan')."
)


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _module(name: str) -> types.ModuleType:
    return types.ModuleType(name)


class _Net:
    def __init__(self, **_kwargs):
        return None


class _FailingUpsampler:
    def __init__(self, **kwargs):
        path = kwargs.get("model_path") or WEIGHT_PATH
        raise RuntimeError(f"weights failed at {path} token={SECRET} local={WEIGHT_PATH}")

    def enhance(self, arr, outscale=1):
        raise AssertionError("enhance should not run after a constructor failure")


class _WorkingUpsampler:
    def __init__(self, **_kwargs):
        return None

    def enhance(self, arr, outscale=1):
        return arr, None


def _install_models(monkeypatch, upsampler):
    realesrgan = _module("realesrgan")
    archs = _module("realesrgan.archs")
    srvgg = _module("realesrgan.archs.srvgg_arch")
    srvgg.SRVGGNetCompact = _Net
    realesrgan.RealESRGANer = upsampler
    realesrgan.archs = archs

    basicsr = _module("basicsr")
    b_archs = _module("basicsr.archs")
    rrdb = _module("basicsr.archs.rrdbnet_arch")
    rrdb.RRDBNet = _Net

    for name, mod in {
        "realesrgan": realesrgan,
        "realesrgan.archs": archs,
        "realesrgan.archs.srvgg_arch": srvgg,
        "basicsr": basicsr,
        "basicsr.archs": b_archs,
        "basicsr.archs.rrdbnet_arch": rrdb,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _block_models(monkeypatch):
    """Import of either package raises ImportError, even if it is installed."""
    for name in (
        "realesrgan",
        "realesrgan.archs",
        "realesrgan.archs.srvgg_arch",
        "basicsr",
        "basicsr.archs",
        "basicsr.archs.rrdbnet_arch",
    ):
        monkeypatch.setitem(sys.modules, name, _module(name))


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(gallery_routes, "require_privilege", lambda _request, _key: "tester")
    app = FastAPI()
    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def test_denoise_exception_is_502_without_exception_text(monkeypatch):
    _install_models(monkeypatch, _FailingUpsampler)
    res = _client(monkeypatch).post("/api/image/denoise", json={"image": _png_b64(), "strength": 0.4})
    assert res.status_code == 502
    assert res.json() == {"error": "Denoise failed"}
    assert SECRET not in res.text
    assert WEIGHT_PATH not in res.text
    assert "github.com" not in res.text


def test_upscale_local_exception_is_502_without_exception_text(monkeypatch):
    _install_models(monkeypatch, _FailingUpsampler)
    res = _client(monkeypatch).post("/api/image/upscale-local", json={"image": _png_b64(), "scale": 4})
    assert res.status_code == 502
    assert res.json() == {"error": "Upscale failed"}
    assert SECRET not in res.text
    assert WEIGHT_PATH not in res.text
    assert "github.com" not in res.text


def test_missing_realesrgan_stays_install_message(monkeypatch):
    _block_models(monkeypatch)
    client = _client(monkeypatch)
    denoise = client.post("/api/image/denoise", json={"image": _png_b64()})
    assert denoise.status_code == 200
    assert denoise.json() == {"error": INSTALL_MSG}
    assert SECRET not in denoise.text
    assert "No module named" not in denoise.text

    upscale = client.post("/api/image/upscale-local", json={"image": _png_b64(), "scale": 2})
    assert upscale.status_code == 200
    assert upscale.json() == {"error": INSTALL_MSG}
    assert "cannot import name" not in upscale.text


def test_success_stays_200_image(monkeypatch):
    _install_models(monkeypatch, _WorkingUpsampler)
    client = _client(monkeypatch)
    denoise = client.post("/api/image/denoise", json={"image": _png_b64(), "strength": 0.2})
    assert denoise.status_code == 200
    body = denoise.json()
    assert "error" not in body
    Image.open(io.BytesIO(base64.b64decode(body["image"])))

    upscale = client.post("/api/image/upscale-local", json={"image": _png_b64(), "scale": 2})
    assert upscale.status_code == 200
    body = upscale.json()
    assert "error" not in body
    Image.open(io.BytesIO(base64.b64decode(body["image"])))


def test_missing_image_is_400_and_does_not_load_model(monkeypatch):
    class _Boom(types.ModuleType):
        def __getattr__(self, _name):
            raise RuntimeError(f"model loaded token={SECRET}")

    boom = _Boom("realesrgan")
    monkeypatch.setitem(sys.modules, "realesrgan", boom)
    monkeypatch.setitem(sys.modules, "basicsr", _Boom("basicsr"))
    client = _client(monkeypatch)
    denoise = client.post("/api/image/denoise", json={})
    upscale = client.post("/api/image/upscale-local", json={"image": ""})
    assert denoise.status_code == 400
    assert upscale.status_code == 400
    assert denoise.json()["detail"] == "No image provided"
    assert upscale.json()["detail"] == "No image provided"
    assert SECRET not in denoise.text
    assert SECRET not in upscale.text
