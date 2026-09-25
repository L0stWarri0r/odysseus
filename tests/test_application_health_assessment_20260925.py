"""Cookbook kill-pid must not answer HTTP 200 with kill stderr or exception text."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.cookbook_routes as cookbook_routes
from routes.cookbook_routes import setup_cookbook_routes

_LEAK = "/home/secret-user/.ssh/id_ed25519: Permission denied TOKEN-kill-stderr"


def _client(monkeypatch, *, windows: bool = False) -> TestClient:
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", windows)
    app = FastAPI()
    app.include_router(setup_cookbook_routes())
    return TestClient(app)


class _Proc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def test_local_kill_failure_is_502_without_stderr(monkeypatch):
    async def _fail(*args, **kwargs):
        return _Proc(1, stderr=_LEAK.encode())

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _fail)
    response = _client(monkeypatch).post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "TERM"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to signal process"}
    assert "secret-user" not in response.text
    assert "TOKEN-kill-stderr" not in response.text


def test_remote_kill_failure_is_502_without_ssh_stderr(monkeypatch):
    async def _fail(*args, **kwargs):
        return _Proc(255, stderr=b"ssh: connect to host gpu.internal.example port 22: TOKEN-ssh-kill")

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_shell", _fail)

    async def _should_not_exec(*args, **kwargs):
        raise AssertionError("remote kill must use the ssh shell path")

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _should_not_exec)
    response = _client(monkeypatch).post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "KILL", "host": "user@gpu.example", "ssh_port": "2222"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to signal process"}
    assert "gpu.internal.example" not in response.text
    assert "TOKEN-ssh-kill" not in response.text


def test_kill_exception_is_502_without_exception_text(monkeypatch):
    async def _boom(*args, **kwargs):
        raise FileNotFoundError("kill missing at /opt/secret-bin/kill")

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _boom)
    response = _client(monkeypatch).post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "INT"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to signal process"}
    assert "secret-bin" not in response.text


def test_kill_timeout_is_502(monkeypatch):
    async def _ok(*args, **kwargs):
        return _Proc(0)

    async def _timeout(coro, *args, **kwargs):
        close = getattr(coro, "close", None)
        if close:
            close()
        raise cookbook_routes.asyncio.TimeoutError

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _ok)
    monkeypatch.setattr(cookbook_routes.asyncio, "wait_for", _timeout)
    response = _client(monkeypatch).post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "TERM"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to signal process"}


def test_local_kill_success_is_200(monkeypatch):
    seen = {}

    async def _ok(*args, **kwargs):
        seen["args"] = args
        return _Proc(0)

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _ok)
    response = _client(monkeypatch).post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "TERM"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "pid": 4242, "signal": "TERM"}
    assert seen["args"][0] == "kill"
    assert seen["args"][1:] == ("-TERM", "4242")


def test_windows_missing_pid_is_404(monkeypatch):
    monkeypatch.setattr(cookbook_routes, "pid_alive", lambda pid: False)

    def _should_not_kill(pid):
        raise AssertionError("must not kill a PID that is not running")

    monkeypatch.setattr(cookbook_routes, "kill_process_tree", _should_not_kill)
    response = _client(monkeypatch, windows=True).post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "TERM"},
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "PID 4242 is not running"}


def test_windows_kill_exception_is_502_without_exception_text(monkeypatch):
    monkeypatch.setattr(cookbook_routes, "pid_alive", lambda pid: True)

    def _boom(pid):
        raise OSError(r"Access denied for \\secret-host\pipe TOKEN-taskkill")

    monkeypatch.setattr(cookbook_routes, "kill_process_tree", _boom)
    response = _client(monkeypatch, windows=True).post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "KILL"},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to signal process"}
    assert "secret-host" not in response.text
    assert "TOKEN-taskkill" not in response.text


def test_windows_kill_success_is_200(monkeypatch):
    monkeypatch.setattr(cookbook_routes, "pid_alive", lambda pid: True)
    seen = {}

    def _kill(pid):
        seen["pid"] = pid

    monkeypatch.setattr(cookbook_routes, "kill_process_tree", _kill)
    response = _client(monkeypatch, windows=True).post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "INT"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "pid": 4242, "signal": "INT"}
    assert seen["pid"] == 4242


def test_kill_pid_validation_stays_400(monkeypatch):
    async def _should_not_run(*args, **kwargs):
        raise AssertionError("kill must not run for an invalid request")

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _should_not_run)
    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_shell", _should_not_run)
    client = _client(monkeypatch)

    low = client.post("/api/cookbook/kill-pid", json={"pid": 1, "signal": "TERM"})
    assert low.status_code == 400
    assert "1" in low.json()["detail"]

    bad_sig = client.post("/api/cookbook/kill-pid", json={"pid": 4242, "signal": "HUP"})
    assert bad_sig.status_code == 400

    bad_host = client.post(
        "/api/cookbook/kill-pid",
        json={"pid": 4242, "signal": "TERM", "host": "-oProxyCommand=evil"},
    )
    assert bad_host.status_code == 400


def test_kill_pid_ui_checks_http_status():
    src = Path("static/js/cookbookServe.js").read_text(encoding="utf-8")
    start = src.index("const _doKill = async")
    fn = src[start:src.index("const _openProbePopup")]
    assert "if (!res.ok || !data.ok)" in fn
    assert "data.error || data.detail" in fn
