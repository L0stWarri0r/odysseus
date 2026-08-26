"""Health-assessment regressions for 2026-08-26.

These pin Hermes privacy/auth gaps and ownership fail-closed behaviour that
were still live on lost/personal-core after prior health-assessment PRs.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.gallery_helpers import generated_image_allowed_for_user, _owner_filter
from routes.hermes_routes import setup_hermes_routes
from src.hermes_control.chat_integration import _apply_hermes_control_policy
from src.hermes_control.models import HermesDecision, HermesRequestContext
from src.hermes_control.policy import evaluate
from src.hermes_control.privacy import find_privacy_signals


class DummySession:
    def __init__(self, endpoint_url, model="dummy-model"):
        self.endpoint_url = endpoint_url
        self.model = model


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


def test_unix_and_macos_home_paths_are_flagged():
    unix = find_privacy_signals("Please open /home/lost/Documents/notes.md")
    macos = find_privacy_signals("The file is at /Users/lost/Projects/odysseus/README.md")
    home = find_privacy_signals("Read ~/Documents/private.txt next")

    assert any(f.type == "local_path" and "Unix" in f.label for f in unix)
    assert any(f.type == "local_path" and "Unix" in f.label for f in macos)
    assert any(f.type == "local_path" and "Unix" in f.label for f in home)


def test_http_url_home_segment_is_not_flagged_as_local_path():
    findings = find_privacy_signals("See https://example.com/home/lost/docs for the API.")
    assert not any(f.type == "local_path" for f in findings)


def test_bare_provider_keys_are_redacted_and_blocked_on_cloud():
    findings = find_privacy_signals("paste sk-proj-abcdefghijklmnopqrstuvwxyz1234567890 here")
    assert any(f.type == "secret" and f.preview == "[REDACTED]" for f in findings)

    result = evaluate(
        HermesRequestContext(
            message="paste sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456 here",
            session_id="s-cloud",
            endpoint_url="https://api.openai.com/v1",
            model="cloud-model",
        )
    )
    assert result.decision == HermesDecision.BLOCK


def test_incognito_nobody_mode_keeps_local_lane_opaque():
    result = _apply_hermes_control_policy(
        message="OPENAI_API_KEY=sk-testsecret12345 /home/lost/secrets.env",
        session_id="s-nobody",
        sess=DummySession("http://127.0.0.1:1234/v1"),
        mode="chat",
        private_mode=False,
        incognito="true",
        use_web="true",
        use_research="true",
        allow_web_search="true",
    )
    assert result.policy.content_visible_to_hermes is False
    assert result.policy.findings == []
    assert result.use_web is False


def test_hermes_inventory_and_preflight_require_admin():
    assert _client_with_user(None).get("/api/hermes/continuity/inventory").status_code == 403
    assert _client_with_user("nonadmin").get("/api/hermes/continuity/inventory").status_code == 403
    assert _client_with_user("nonadmin").post(
        "/api/hermes/preflight",
        json={"message": "hi", "endpoint_url": "https://api.openai.com/v1"},
    ).status_code == 403

    inventory = _client_with_user("admin").get("/api/hermes/continuity/inventory")
    assert inventory.status_code == 200
    assert inventory.json()["content_returned"] is False

    preflight = _client_with_user("admin").post(
        "/api/hermes/preflight",
        json={"message": "Explain memory.", "endpoint_url": "https://api.openai.com/v1"},
    )
    assert preflight.status_code == 200
    assert preflight.json()["decision"] == "allow"


def test_generated_image_null_owner_is_not_world_readable():
    row = SimpleNamespace(owner=None)
    assert generated_image_allowed_for_user("alice", row, auth_disabled=False) is False
    assert generated_image_allowed_for_user("alice", SimpleNamespace(owner="bob"), auth_disabled=False) is False
    assert generated_image_allowed_for_user("alice", SimpleNamespace(owner="alice"), auth_disabled=False) is True
    assert generated_image_allowed_for_user(None, SimpleNamespace(owner="alice"), auth_disabled=False) is False
    assert generated_image_allowed_for_user("alice", None, auth_disabled=False) is True
    assert generated_image_allowed_for_user(None, row, auth_disabled=True) is True


def test_gallery_owner_filter_fails_closed_when_auth_is_on(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    fake_q = MagicMock()
    out = _owner_filter(fake_q, user=None)
    fake_q.filter.assert_called_once_with(False)
    assert out is fake_q.filter.return_value


def test_gallery_owner_filter_unfiltered_when_auth_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    fake_q = MagicMock()
    out = _owner_filter(fake_q, user=None)
    fake_q.filter.assert_not_called()
    assert out is fake_q
