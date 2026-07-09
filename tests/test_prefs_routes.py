import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.prefs_routes as prefs_routes


class FakeAuthManager:
    is_configured = True


def _client_with_user(username=None):
    app = FastAPI()
    app.state.auth_manager = FakeAuthManager()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(prefs_routes.setup_prefs_routes())
    return TestClient(app)


def test_load_ignores_non_object_prefs_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps(["not", "a", "prefs", "object"]), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    assert prefs_routes._load() == {}
    assert prefs_routes._load_for_user("alice") == {}


def test_load_keeps_object_prefs_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    assert prefs_routes._load_for_user("alice") == {"theme": "dark"}


def test_prefs_routes_require_user_when_auth_configured(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps({"_users": {"alice": {"theme": "dark"}}}), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    assert _client_with_user(None).get("/api/prefs").status_code == 401
    assert _client_with_user(None).get("/api/prefs/theme").status_code == 401
    assert _client_with_user(None).put("/api/prefs/theme", json={"value": "light"}).status_code == 401


def test_prefs_routes_use_current_user(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps({"_users": {"alice": {"theme": "dark"}}}), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    response = _client_with_user("alice").get("/api/prefs")

    assert response.status_code == 200
    assert response.json() == {"theme": "dark"}
