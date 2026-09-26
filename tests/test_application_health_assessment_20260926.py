"""Cookbook state save must not answer HTTP 200 with exception text."""

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.cookbook_routes as cookbook_routes
from routes.cookbook_routes import setup_cookbook_routes

_LEAK = "Permission denied: /home/secret-user/data/cookbook_state.json TOKEN-state-write"


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(setup_cookbook_routes())
    return TestClient(app)


def test_state_write_failure_is_502_without_exception_text(monkeypatch, tmp_path):
    sentinel = tmp_path / "cookbook_state.json"
    sentinel.write_text('{"tasks":[{"sessionId":"keep-me"}]}', encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError(_LEAK)

    monkeypatch.setattr("core.atomic_io.atomic_write_json", _boom)
    response = _client(monkeypatch, tmp_path).post(
        "/api/cookbook/state",
        json={"tasks": [], "env": {"servers": [{"host": ""}]}},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Failed to save cookbook state"}
    assert "secret-user" not in response.text
    assert "TOKEN-state-write" not in response.text
    assert sentinel.read_text(encoding="utf-8") == '{"tasks":[{"sessionId":"keep-me"}]}'


def test_invalid_json_is_400_without_body(monkeypatch, tmp_path):
    response = _client(monkeypatch, tmp_path).post(
        "/api/cookbook/state",
        content=b"{not-json TOKEN-state-body",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Invalid cookbook state"}
    assert "TOKEN-state-body" not in response.text
    assert not (tmp_path / "cookbook_state.json").exists()


def test_invalid_token_stays_400(monkeypatch, tmp_path):
    response = _client(monkeypatch, tmp_path).post(
        "/api/cookbook/state",
        json={"env": {"hfToken": "bad token!"}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid token characters"
    assert "bad token" not in response.text
    assert not (tmp_path / "cookbook_state.json").exists()


def test_state_save_success_is_200(monkeypatch, tmp_path):
    response = _client(monkeypatch, tmp_path).post(
        "/api/cookbook/state",
        json={
            "tasks": [{"sessionId": "serve-abc", "type": "serve", "status": "running"}],
            "env": {"servers": [{"host": ""}]},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "preserved": 0}
    saved = json.loads((tmp_path / "cookbook_state.json").read_text(encoding="utf-8"))
    assert saved["tasks"][0]["sessionId"] == "serve-abc"
    assert saved["env"]["servers"][0]["host"] == ""


def test_agent_state_write_checks_http_status():
    src = Path("src/tool_implementations.py").read_text(encoding="utf-8")
    start = src.index("async def _cookbook_register_task")
    fn = src[start:src.index("_APP_API_BLOCKLIST_PREFIXES")]
    assert "return r.status_code < 400" in fn


def test_state_sync_does_not_surface_error_body():
    src = Path("static/js/cookbookRunning.js").read_text(encoding="utf-8")
    start = src.index("function _syncToServer()")
    fn = src[start:src.index("function _normalizeState")]
    assert "/api/cookbook/state" in fn
    assert "data.error" not in fn
