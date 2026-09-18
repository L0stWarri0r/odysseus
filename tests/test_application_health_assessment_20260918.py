"""Health assessment 2026-09-18: ntfy/integration tests must not look like success."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from routes.auth_routes import SESSION_COOKIE, integration_error, setup_auth_routes

_REPO = Path(__file__).resolve().parents[1]
_AUTH_ROUTES = (_REPO / "routes" / "auth_routes.py").read_text(encoding="utf-8")
_SETTINGS_JS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")


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


def _admin_request(token="session-token"):
    req = SimpleNamespace()
    req.cookies = {SESSION_COOKIE: token}
    req.client = SimpleNamespace(host="127.0.0.1")
    return req


class _FakeResp:
    def __init__(self, status_code=200, text="", is_success=True):
        self.status_code = status_code
        self.text = text
        self.is_success = is_success


class _FakeClient:
    def __init__(self, resp=None, error=None):
        self._resp = resp
        self._error = error
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        self.posts.append((url, content, headers))
        if self._error:
            raise self._error
        return self._resp


@pytest.fixture
def test_integration():
    auth_manager = MagicMock()
    auth_manager.get_username_for_token.return_value = "admin"
    auth_manager.is_admin.return_value = True
    router = setup_auth_routes(auth_manager)
    endpoint = _route_endpoint(
        router, "/api/auth/integrations/{integration_id}/test", "POST"
    )
    return auth_manager, endpoint


def test_integration_error_helper_returns_json_error_with_non_200_status():
    assert "def integration_error(message: str, status_code: int = 502)" in _AUTH_ROUTES
    resp = integration_error("ntfy publish failed")
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body == {"ok": False, "message": "ntfy publish failed"}


def test_ntfy_test_uses_integration_error_instead_of_http_200():
    assert "return integration_error(" in _AUTH_ROUTES
    assert 'f"ntfy returned HTTP {r.status_code} from {full_url}: {r.text[:200]}"' not in _AUTH_ROUTES
    assert 'f"ntfy publish to {full_url} failed: {e}.{hint}"' not in _AUTH_ROUTES
    assert 'return {"ok": False, "message": (result.get("error") or "Connection failed")[:300]}' not in _AUTH_ROUTES
    assert "follow_redirects=False, trust_env=False" in _AUTH_ROUTES


def test_integrations_ui_does_not_treat_test_failures_as_success():
    assert "const ok = res.ok && data.ok;" in _SETTINGS_JS
    assert "if (r.ok && d.ok)" in _SETTINGS_JS
    assert "statusEl.style.color = data.ok ? 'var(--green, #98c379)' : 'var(--red)';" not in _SETTINGS_JS
    assert "if (d.ok) {\n          el('uf-api-msg').textContent = d.message || 'Connected';" not in _SETTINGS_JS


@pytest.mark.asyncio
async def test_ntfy_http_failure_is_http_502_without_response_body(test_integration, monkeypatch):
    auth_manager, endpoint = test_integration
    fake = _FakeClient(
        resp=_FakeResp(status_code=403, text="secret-ntfy-body xyz", is_success=False)
    )
    client_kwargs = {}

    def _client(**kwargs):
        client_kwargs.update(kwargs)
        return fake

    import httpx
    import routes.auth_routes as auth_routes

    monkeypatch.setattr(
        auth_routes,
        "get_integration",
        lambda _id: {
            "id": "ntfy1",
            "preset": "ntfy",
            "name": "ntfy",
            "base_url": "http://127.0.0.1:8091",
            "api_key": "",
            "auth_type": "none",
        },
    )
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: {"reminder_ntfy_topic": "reminders"})
    monkeypatch.setattr(httpx, "AsyncClient", _client)

    status, body = _payload(await endpoint("ntfy1", _admin_request()))
    assert status == 502
    assert body == {
        "ok": False,
        "message": "ntfy returned HTTP 403 from http://127.0.0.1:8091/reminders",
    }
    assert "secret-ntfy-body" not in json.dumps(body)
    assert client_kwargs.get("follow_redirects") is False
    assert client_kwargs.get("trust_env") is False
    assert fake.posts[0][0] == "http://127.0.0.1:8091/reminders"


@pytest.mark.asyncio
async def test_ntfy_exception_is_http_502_without_exception_text(test_integration, monkeypatch):
    auth_manager, endpoint = test_integration
    fake = _FakeClient(error=RuntimeError("proxy-secret-token leaked"))

    def _client(**kwargs):
        return fake

    import httpx
    import routes.auth_routes as auth_routes

    monkeypatch.setattr(
        auth_routes,
        "get_integration",
        lambda _id: {
            "id": "ntfy1",
            "preset": "ntfy",
            "base_url": "http://100.64.1.2:8091",
            "api_key": "",
            "auth_type": "none",
        },
    )
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: {"reminder_ntfy_topic": "alerts"})
    monkeypatch.setattr(httpx, "AsyncClient", _client)

    status, body = _payload(await endpoint("ntfy1", _admin_request()))
    assert status == 502
    assert body["ok"] is False
    assert body["message"].startswith("ntfy publish to http://100.64.1.2:8091/alerts failed.")
    assert "NTFY_BIND" in body["message"]
    assert "proxy-secret-token" not in json.dumps(body)


@pytest.mark.asyncio
async def test_ntfy_success_still_returns_200(test_integration, monkeypatch):
    auth_manager, endpoint = test_integration
    fake = _FakeClient(resp=_FakeResp(status_code=200, text="ok", is_success=True))

    def _client(**kwargs):
        return fake

    import httpx
    import routes.auth_routes as auth_routes

    monkeypatch.setattr(
        auth_routes,
        "get_integration",
        lambda _id: {
            "id": "ntfy1",
            "preset": "ntfy",
            "base_url": "http://127.0.0.1:8091/extra-path",
            "api_key": "",
            "auth_type": "none",
        },
    )
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: {"reminder_ntfy_topic": "Reminders"})
    monkeypatch.setattr(httpx, "AsyncClient", _client)

    status, body = _payload(await endpoint("ntfy1", _admin_request()))
    assert status == 200
    assert body["ok"] is True
    assert "http://127.0.0.1:8091/Reminders" in body["message"]
    assert fake.posts[0][0] == "http://127.0.0.1:8091/Reminders"


@pytest.mark.asyncio
async def test_generic_integration_failure_is_http_502_without_upstream_body(
    test_integration, monkeypatch
):
    auth_manager, endpoint = test_integration
    import routes.auth_routes as auth_routes

    monkeypatch.setattr(
        auth_routes,
        "get_integration",
        lambda _id: {
            "id": "mini1",
            "preset": "miniflux",
            "base_url": "http://127.0.0.1:8080",
        },
    )

    async def _fail(*a, **k):
        return {"exit_code": 1, "error": "HTTP 401\nsecret-miniflux-body"}

    monkeypatch.setattr(auth_routes, "execute_api_call", _fail)
    status, body = _payload(await endpoint("mini1", _admin_request()))
    assert status == 502
    assert body == {"ok": False, "message": "Connection failed"}
    assert "secret-miniflux-body" not in json.dumps(body)


@pytest.mark.asyncio
async def test_generic_integration_success_still_returns_200(test_integration, monkeypatch):
    auth_manager, endpoint = test_integration
    import routes.auth_routes as auth_routes

    monkeypatch.setattr(
        auth_routes,
        "get_integration",
        lambda _id: {
            "id": "mini1",
            "preset": "miniflux",
            "base_url": "http://127.0.0.1:8080",
        },
    )

    async def _ok(*a, **k):
        return {"exit_code": 0, "output": "HTTP 200"}

    monkeypatch.setattr(auth_routes, "execute_api_call", _ok)
    status, body = _payload(await endpoint("mini1", _admin_request()))
    assert status == 200
    assert body == {"ok": True, "message": "Connection successful"}


@pytest.mark.asyncio
async def test_integration_test_missing_id_is_http_404(test_integration, monkeypatch):
    auth_manager, endpoint = test_integration
    import routes.auth_routes as auth_routes

    monkeypatch.setattr(auth_routes, "get_integration", lambda _id: None)
    with pytest.raises(HTTPException) as exc:
        await endpoint("missing", _admin_request())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_integration_test_requires_admin(test_integration):
    auth_manager, endpoint = test_integration
    auth_manager.get_username_for_token.return_value = "bob"
    auth_manager.is_admin.return_value = False
    with pytest.raises(HTTPException) as exc:
        await endpoint("ntfy1", _admin_request())
    assert exc.value.status_code == 403
