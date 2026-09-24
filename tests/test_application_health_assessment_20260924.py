"""Cookbook SSH key generation must not answer HTTP 200 with ssh-keygen stderr."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.cookbook_routes as cookbook_routes
from routes.cookbook_routes import setup_cookbook_routes

_LEAK = "/home/secret-user/id_ed25519: Permission denied by policy TOKEN-ssh-keygen-stderr"


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", True)
    monkeypatch.setattr(cookbook_routes.Path, "home", classmethod(lambda cls: tmp_path))
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


def test_ssh_keygen_failure_is_502_without_stderr(monkeypatch, tmp_path):
    async def _fail(*args, **kwargs):
        return _Proc(1, stderr=_LEAK.encode())

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _fail)
    response = _client(monkeypatch, tmp_path).post("/api/cookbook/ssh-key")

    assert response.status_code == 502
    body = response.json()
    assert body == {"ok": False, "error": "Failed to generate SSH key"}
    assert "secret-user" not in response.text
    assert "TOKEN-ssh-keygen-stderr" not in response.text


def test_ssh_keygen_exception_is_502_without_exception_text(monkeypatch, tmp_path):
    async def _boom(*args, **kwargs):
        raise FileNotFoundError("ssh-keygen missing at /opt/secret-bin/ssh-keygen")

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _boom)
    response = _client(monkeypatch, tmp_path).post("/api/cookbook/ssh-key")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to generate SSH key"}
    assert "secret-bin" not in response.text


def test_ssh_key_generation_success_returns_public_key(monkeypatch, tmp_path):
    public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey odysseus-cookbook"

    async def _ok(*args, **kwargs):
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        (ssh_dir / "id_ed25519").write_text("private-key", encoding="utf-8")
        (ssh_dir / "id_ed25519.pub").write_text(public + "\n", encoding="utf-8")
        return _Proc(0)

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _ok)
    response = _client(monkeypatch, tmp_path).post("/api/cookbook/ssh-key")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "public_key": public}


def test_existing_ssh_key_skips_ssh_keygen(monkeypatch, tmp_path):
    public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExisting odysseus-cookbook"
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("private-key", encoding="utf-8")
    (ssh_dir / "id_ed25519.pub").write_text(public + "\n", encoding="utf-8")

    async def _should_not_run(*args, **kwargs):
        raise AssertionError("ssh-keygen should not run when the key already exists")

    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_exec", _should_not_run)
    response = _client(monkeypatch, tmp_path).post("/api/cookbook/ssh-key")

    assert response.status_code == 200
    assert response.json()["public_key"] == public


def test_missing_public_key_is_502(monkeypatch, tmp_path):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("private-key", encoding="utf-8")

    response = _client(monkeypatch, tmp_path).post("/api/cookbook/ssh-key")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to generate SSH key"}


def test_cookbook_ssh_key_ui_checks_http_status():
    src = Path("static/js/cookbook-hwfit.js").read_text(encoding="utf-8")
    start = src.index("async function _fetchCookbookSshKey")
    fn = src[start:src.index("async function _populateServerKeyPanel")]
    assert "if (!res.ok || (generate && !data.ok))" in fn
    assert "res.json().catch" in fn
