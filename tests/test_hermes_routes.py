from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.hermes_routes import setup_hermes_routes
from src.hermes_control.routing import is_local_endpoint


class FakeAuthManager:
    is_configured = True

    def is_admin(self, username):
        return username == "admin"


def _client_with_user(username=None):
    app = FastAPI()
    app.state.auth_manager = FakeAuthManager()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(setup_hermes_routes())
    return TestClient(app)


def test_hermes_preflight_allows_normal_request():
    client = _client_with_user("admin")

    response = client.post(
        "/api/hermes/preflight",
        json={
            "message": "Explain ChromaDB memory in plain English.",
            "session_id": "s1",
            "endpoint_url": "https://api.openai.com/v1",
            "model": "cloud-model",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "allow"
    assert data["content_visible_to_hermes"] is True


def test_hermes_preflight_private_local_content_is_opaque():
    client = _client_with_user("admin")

    response = client.post(
        "/api/hermes/preflight",
        json={
            "message": "OPENAI_API_KEY=sk-test-sensitive C:\\Users\\Chase\\Documents\\private.txt",
            "session_id": "s-private",
            "endpoint_url": "http://127.0.0.1:1234/v1",
            "model": "local-model",
            "private_mode": True,
            "use_web": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "allow_with_adjustments"
    assert data["content_visible_to_hermes"] is False
    assert data["findings"] == []
    assert data["adjusted_context"]["use_web"] is False
    assert "disable_web" in data["actions"]


def test_hermes_preflight_requires_admin():
    assert _client_with_user(None).post(
        "/api/hermes/preflight",
        json={
            "message": "hello",
            "session_id": "s1",
            "endpoint_url": "https://api.openai.com/v1",
            "model": "cloud-model",
        },
    ).status_code == 403
    assert _client_with_user("nonadmin").post(
        "/api/hermes/preflight",
        json={
            "message": "hello",
            "session_id": "s1",
            "endpoint_url": "https://api.openai.com/v1",
            "model": "cloud-model",
        },
    ).status_code == 403


def test_is_local_endpoint_recognizes_loopback_variants():
    assert is_local_endpoint("http://127.0.0.1:1234/v1") is True
    assert is_local_endpoint("http://localhost:1234/v1") is True
    assert is_local_endpoint("http://localhost.:1234/v1") is True
    assert is_local_endpoint("http://127.1:1234/v1") is True
    assert is_local_endpoint("http://[::1]:1234/v1") is True
    assert is_local_endpoint("http://[::ffff:127.0.0.1]:1234/v1") is True
    assert is_local_endpoint("https://api.openai.com/v1") is False
    assert is_local_endpoint("http://192.168.0.5:1234/v1") is False
