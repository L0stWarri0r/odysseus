"""Health assessment 2026-09-21: cookbook SSH/GPU probes must not look like success."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.responses import JSONResponse

from routes.cookbook_routes import cookbook_error, setup_cookbook_routes

_REPO = Path(__file__).resolve().parents[1]
_COOKBOOK_ROUTES = (_REPO / "routes" / "cookbook_routes.py").read_text(encoding="utf-8")
_HWFIT_JS = (_REPO / "static" / "js" / "cookbook-hwfit.js").read_text(encoding="utf-8")
_SERVE_JS = (_REPO / "static" / "js" / "cookbookServe.js").read_text(encoding="utf-8")


def _setup_source() -> str:
    start = _COOKBOOK_ROUTES.index("async def server_setup")
    end = _COOKBOOK_ROUTES.index("async def _run_nvidia_smi", start)
    return _COOKBOOK_ROUTES[start:end]


def _gpus_source() -> str:
    start = _COOKBOOK_ROUTES.index("async def list_gpus")
    end = _COOKBOOK_ROUTES.index("class KillPidRequest", start)
    return _COOKBOOK_ROUTES[start:end]


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _payload(res):
    if isinstance(res, JSONResponse):
        return res.status_code, json.loads(res.body)
    return 200, res


class _Req:
    def __init__(self, *, user="admin"):
        self.state = SimpleNamespace(current_user=user)
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.headers = {}


class _Proc:
    def __init__(self, rc=0, stdout=b"", stderr=b""):
        self.returncode = rc
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass


def test_cookbook_error_helper_returns_json_error_with_non_200_status():
    assert "def cookbook_error(message: str, status_code: int = 502, **extra)" in _COOKBOOK_ROUTES
    resp = cookbook_error("Setup failed", platform="linux")
    assert resp.status_code == 502
    assert json.loads(resp.body) == {"ok": False, "error": "Setup failed", "platform": "linux"}


def test_setup_uses_cookbook_error_instead_of_http_200():
    src = _setup_source()
    assert 'return cookbook_error("Setup failed", platform=platform)' in src
    assert 'return cookbook_error("Setup timed out (120s)", platform=platform)' in src
    assert 'return {"ok": False, "error": str(e), "platform": platform}' not in src
    assert 'return {"ok": False, "error": "Setup timed out (120s)", "platform": platform}' not in src
    assert 'ok = "OK" in output' not in src


def test_gpus_uses_cookbook_error_instead_of_http_200():
    src = _gpus_source()
    assert 'return cookbook_error("No GPU memory probe available", gpus=[])' in src
    assert 'nvidia_error = str(e)[:200]' not in src
    assert '"nvidia_error": nvidia_error' not in src
    assert (
        'return {"ok": False, "error": nvidia_error or "No GPU memory probe available", "gpus": []}'
        not in src
    )


def test_setup_ui_does_not_treat_http_errors_as_success():
    assert "if (res.ok && data.ok)" in _HWFIT_JS
    assert "const data = await res.json();\n          if (data.ok)" not in _HWFIT_JS


def test_gpu_probe_ui_does_not_treat_http_errors_as_success():
    assert "if (!res.ok)" in _SERVE_JS
    assert "if (!res.ok || !data.ok)" in _SERVE_JS
    assert "if (!res.ok || data.ok === false)" in _SERVE_JS


@pytest.fixture
def cookbook_router(monkeypatch):
    import routes.cookbook_routes as cookbook_routes

    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes.sys, "platform", "linux")
    return setup_cookbook_routes()


@pytest.fixture
def server_setup(cookbook_router):
    return _route_endpoint(cookbook_router, "/api/cookbook/setup", "POST")


@pytest.fixture
def list_gpus(cookbook_router):
    return _route_endpoint(cookbook_router, "/api/cookbook/gpus", "GET")


@pytest.mark.asyncio
async def test_setup_exception_is_http_502_without_exception_text(server_setup, monkeypatch):
    import asyncio

    async def _boom(*a, **k):
        raise RuntimeError("secret-ssh-key-xyz leaked")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _boom)
    status, body = _payload(await server_setup(_Req(), SimpleNamespace(host="user@host.example", ssh_port=None)))
    assert status == 502
    assert body == {"ok": False, "error": "Setup failed", "platform": "linux"}
    assert "secret-ssh-key-xyz" not in json.dumps(body)


@pytest.mark.asyncio
async def test_setup_timeout_is_http_502(server_setup, monkeypatch):
    import asyncio

    async def _timeout(aw, timeout=None):
        if hasattr(aw, "close"):
            aw.close()
        raise asyncio.TimeoutError()

    async def _proc(*a, **k):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _proc)
    monkeypatch.setattr(asyncio, "wait_for", _timeout)
    status, body = _payload(await server_setup(_Req(), SimpleNamespace(host="user@host.example", ssh_port=None)))
    assert status == 502
    assert body == {"ok": False, "error": "Setup timed out (120s)", "platform": "linux"}


@pytest.mark.asyncio
async def test_setup_missing_ok_is_http_502_without_stderr(server_setup, monkeypatch):
    import asyncio

    calls = {"n": 0}

    async def _shell(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Proc(stdout=b"linux\n")
        return _Proc(rc=1, stdout=b"", stderr=b"secret-setup-stderr /root/.ssh/id_ed25519")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _shell)
    status, body = _payload(await server_setup(_Req(), SimpleNamespace(host="user@host.example", ssh_port=None)))
    assert status == 502
    assert body == {"ok": False, "error": "Setup failed", "platform": "linux"}
    assert "secret-setup-stderr" not in json.dumps(body)
    assert "output" not in body


@pytest.mark.asyncio
async def test_setup_success_still_returns_200(server_setup, monkeypatch):
    import asyncio

    calls = {"n": 0}

    async def _shell(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Proc(stdout=b"linux\n")
        return _Proc(stdout=b"from huggingface_hub import snapshot_download\nOK\n")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _shell)
    status, body = _payload(await server_setup(_Req(), SimpleNamespace(host="user@host.example", ssh_port=None)))
    assert status == 200
    assert body["ok"] is True
    assert body["platform"] == "linux"
    assert "OK" in body["output"]


@pytest.mark.asyncio
async def test_gpus_probe_failure_is_http_502_without_stderr(list_gpus, monkeypatch):
    import asyncio

    async def _exec(*a, **k):
        return _Proc(rc=1, stderr=b"secret-nvidia-stderr /root/.ssh/id_ed25519")

    async def _shell(*a, **k):
        return _Proc(rc=1, stderr=b"secret-amd-stderr")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _shell)
    status, body = _payload(await list_gpus(_Req()))
    assert status == 502
    assert body == {"ok": False, "error": "No GPU memory probe available", "gpus": []}
    assert "secret-nvidia-stderr" not in json.dumps(body)
    assert "secret-amd-stderr" not in json.dumps(body)
    assert "nvidia_error" not in body


@pytest.mark.asyncio
async def test_gpus_exception_is_http_502_without_exception_text(list_gpus, monkeypatch):
    import asyncio

    async def _exec(*a, **k):
        raise RuntimeError("secret-api-key-xyz leaked")

    async def _shell(*a, **k):
        return _Proc(rc=1, stderr=b"secret-shell-stderr")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _shell)
    status, body = _payload(await list_gpus(_Req()))
    assert status == 502
    assert body == {"ok": False, "error": "No GPU memory probe available", "gpus": []}
    assert "secret-api-key-xyz" not in json.dumps(body)
    assert "secret-shell-stderr" not in json.dumps(body)
    assert "nvidia_error" not in body


@pytest.mark.asyncio
async def test_gpus_success_still_returns_200(list_gpus, monkeypatch):
    import asyncio

    async def _exec(*a, **k):
        joined = " ".join(str(x) for x in a)
        if "query-gpu" in joined:
            return _Proc(stdout=b"0, NVIDIA GeForce RTX 4090, 8000, 24576, 16576, 12, GPU-abc\n")
        return _Proc(stdout=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", MagicMock(side_effect=AssertionError("ssh not used")))
    status, body = _payload(await list_gpus(_Req()))
    assert status == 200
    assert body["ok"] is True
    assert body["backend"] == "cuda"
    assert body["gpus"][0]["name"] == "NVIDIA GeForce RTX 4090"
    assert body["gpus"][0]["free_mb"] == 8000
    assert "nvidia_error" not in body
