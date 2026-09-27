"""Model download and serve launch failures must not answer HTTP 200 with stderr."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.cookbook_routes as cookbook_routes
from routes.cookbook_routes import setup_cookbook_routes

_LEAK = "ssh: /home/secret-user/.ssh/id_ed25519 TOKEN-launch-stderr Permission denied"


class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes):
        self.returncode = returncode
        self.stderr = _FakeStream(stderr)

    async def wait(self):
        return self.returncode


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", False)
    app = FastAPI()
    app.include_router(setup_cookbook_routes())
    return TestClient(app)


def _tmux_present(monkeypatch):
    monkeypatch.setattr(cookbook_routes.shutil, "which", lambda binary: "/usr/bin/" + binary)


def _shell(monkeypatch, *, returncode: int = 1, stderr: bytes = _LEAK.encode()):
    async def _fake_shell(*args, **kwargs):
        return _FakeProc(returncode, stderr)

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_shell", _fake_shell)


def test_download_launch_failure_is_502_without_stderr(monkeypatch, tmp_path):
    _tmux_present(monkeypatch)
    _shell(monkeypatch)
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/download",
        json={"repo_id": "org/Model"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to start model download"}
    assert "secret-user" not in response.text
    assert "TOKEN-launch-stderr" not in response.text
    assert "session_id" not in response.json()


def test_download_launch_exception_is_502_without_exception_text(monkeypatch, tmp_path):
    _tmux_present(monkeypatch)

    async def _boom(*args, **kwargs):
        raise OSError(_LEAK)

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_shell", _boom)
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/download",
        json={"repo_id": "org/Model"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to start model download"}
    assert "secret-user" not in response.text
    assert "TOKEN-launch-stderr" not in response.text


def test_windows_download_launch_exception_is_502(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise OSError(_LEAK)

    monkeypatch.setattr(cookbook_routes.subprocess, "Popen", _boom)
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", True)
    response = client.post(
        "/api/model/download",
        json={"repo_id": "org/Model"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to start model download"}
    assert "secret-user" not in response.text
    assert "TOKEN-launch-stderr" not in response.text


def test_missing_tmux_download_stays_200_with_install_message(monkeypatch, tmp_path):
    monkeypatch.setattr(cookbook_routes.shutil, "which", lambda binary: None)
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/download",
        json={"repo_id": "org/Model"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"].startswith("tmux is required")
    assert body["session_id"].startswith("cookbook-")
    assert "TOKEN-launch-stderr" not in response.text


def test_download_success_is_200(monkeypatch, tmp_path):
    _tmux_present(monkeypatch)
    _shell(monkeypatch, returncode=0, stderr=b"")
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/download",
        json={"repo_id": "org/Model"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["session_id"].startswith("cookbook-")
    assert body["remote"] == "local"


def test_invalid_download_repo_stays_400(monkeypatch, tmp_path):
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/download",
        json={"repo_id": "not a repo"},
    )

    assert response.status_code == 400
    assert "Invalid repo_id" in response.json()["detail"]


def test_serve_launch_failure_is_502_without_stderr(monkeypatch, tmp_path):
    _tmux_present(monkeypatch)
    _shell(monkeypatch)
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/serve",
        json={"repo_id": "org/Model", "cmd": "python3 -m llama_cpp.server --port 8080"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to start model server"}
    assert "secret-user" not in response.text
    assert "TOKEN-launch-stderr" not in response.text
    assert "session_id" not in response.json()


def test_serve_launch_exception_is_502_without_exception_text(monkeypatch, tmp_path):
    _tmux_present(monkeypatch)

    async def _boom(*args, **kwargs):
        raise OSError(_LEAK)

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_shell", _boom)
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/serve",
        json={"repo_id": "org/Model", "cmd": "python3 -m llama_cpp.server --port 8080"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to start model server"}
    assert "secret-user" not in response.text
    assert "TOKEN-launch-stderr" not in response.text


def test_windows_serve_launch_exception_is_502(monkeypatch, tmp_path):
    def _boom(*args, **kwargs):
        raise OSError(_LEAK)

    monkeypatch.setattr(cookbook_routes.subprocess, "Popen", _boom)
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", True)
    response = client.post(
        "/api/model/serve",
        json={"repo_id": "org/Model", "cmd": "python3 -m llama_cpp.server --port 8080"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to start model server"}
    assert "secret-user" not in response.text
    assert "TOKEN-launch-stderr" not in response.text


def test_missing_tmux_serve_stays_200_with_install_message(monkeypatch, tmp_path):
    monkeypatch.setattr(cookbook_routes.shutil, "which", lambda binary: None)
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/serve",
        json={"repo_id": "org/Model", "cmd": "python3 -m llama_cpp.server --port 8080"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"].startswith("tmux is required")
    assert body["session_id"].startswith("serve-")


def test_serve_success_is_200(monkeypatch, tmp_path):
    _tmux_present(monkeypatch)
    _shell(monkeypatch, returncode=0, stderr=b"")
    response = _client(monkeypatch, tmp_path).post(
        "/api/model/serve",
        json={"repo_id": "huggingface_hub", "cmd": "python3 -m pip install huggingface_hub"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["session_id"].startswith("serve-")
    assert body["remote"] == "local"
    assert body["endpoint_id"] is None


def test_download_and_serve_clients_treat_failure_as_failure():
    download = Path("static/js/cookbookDownload.js").read_text(encoding="utf-8")
    start = download.index("const res = await fetch('/api/model/download'")
    snippet = download[start:download.index("// ── Init", start)]
    assert "if (!res.ok)" in snippet
    assert "if (!data.ok)" in snippet

    running = Path("static/js/cookbookRunning.js").read_text(encoding="utf-8")
    queued = running[running.index("async function _startQueuedDownload"):running.index("// ── Retry download")]
    assert "if (!res.ok)" in queued
    assert "if (!data.ok)" in queued
    retry = running[running.index("async function _retryDownload"):running.index("// ── Serve auto-fix")]
    assert "if (!res.ok)" in retry
    assert "if (!data.ok)" in retry
    launch = running[running.index("export async function _launchServeTask"):running.index("// ── Render Running tab")]
    assert "if (!data.ok)" in launch

    hwfit = Path("static/js/cookbook-hwfit.js").read_text(encoding="utf-8")
    quick = hwfit[hwfit.index("const res = await fetch('/api/model/serve'"):hwfit.index("quickRunBtn.disabled = false")]
    assert "if (data.ok)" in quick
    assert "data.error" in quick

    tools = Path("src/tool_implementations.py").read_text(encoding="utf-8")
    download_tool = tools[tools.index("async def do_download_model"):tools.index("async def do_serve_model")]
    serve_tool = tools[tools.index("async def do_serve_model"):tools.index("async def do_list_served_models")]
    assert 'data.get("ok")' in download_tool
    assert 'data.get("error", "Download failed")' in download_tool
    assert 'data.get("ok")' in serve_tool
    assert 'data.get("error", "Serve failed")' in serve_tool
