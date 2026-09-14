"""Health assessment 2026-09-14: contacts create/update/delete/import failures must not look like success."""

import json
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse


_REPO = Path(__file__).resolve().parents[1]
_CONTACTS_ROUTES = (_REPO / "routes" / "contacts_routes.py").read_text(encoding="utf-8")
_SETTINGS_JS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")
_EMAIL_LIBRARY = (_REPO / "static" / "js" / "emailLibrary.js").read_text(encoding="utf-8")


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


@pytest.fixture
def contact_routes(monkeypatch):
    import routes.contacts_routes as contacts_routes

    monkeypatch.setattr(contacts_routes, "_fetch_contacts", lambda *a, **k: [])
    router = contacts_routes.setup_contacts_routes()
    return {
        "add": _route_endpoint(router, "/api/contacts/add", "POST"),
        "import": _route_endpoint(router, "/api/contacts/import", "POST"),
        "edit": _route_endpoint(router, "/api/contacts/{uid}", "PUT"),
        "delete": _route_endpoint(router, "/api/contacts/{uid}", "DELETE"),
        "module": contacts_routes,
    }


def test_contact_error_helper_returns_json_error_with_non_200_status():
    assert "def contact_error(message: str, status_code: int = 502)" in _CONTACTS_ROUTES
    resp = JSONResponse({"success": False, "error": "Failed to save contact"}, status_code=502)
    assert resp.status_code == 502
    compact = resp.body.replace(b" ", b"")
    assert b'"success":false' in compact or b'"success": false' in resp.body


def test_contact_routes_use_contact_error_instead_of_http_200():
    assert 'return contact_error("Email required", 400)' in _CONTACTS_ROUTES
    assert 'return contact_error("Failed to save contact")' in _CONTACTS_ROUTES
    assert 'return contact_error("Name or email required", 400)' in _CONTACTS_ROUTES
    assert 'return contact_error("Failed to update contact")' in _CONTACTS_ROUTES
    assert 'return contact_error("UID required", 400)' in _CONTACTS_ROUTES
    assert 'return contact_error("Failed to delete contact")' in _CONTACTS_ROUTES
    assert 'return contact_error("No vCard data found", 400)' in _CONTACTS_ROUTES
    assert 'return contact_error("No contact data found", 400)' in _CONTACTS_ROUTES
    assert "status_code=502" in _CONTACTS_ROUTES
    assert 'return {"success": ok}' not in _CONTACTS_ROUTES


def test_contacts_ui_does_not_treat_failures_as_empty_success():
    assert "if (!res.ok || d.success === false)" in _SETTINGS_JS
    assert "Failed to add contact" in _SETTINGS_JS
    assert "Failed to update contact" in _SETTINGS_JS
    assert "Failed to delete contact" in _SETTINGS_JS
    assert "if (!r.ok || d.error)" in _SETTINGS_JS
    assert "if (!r.ok || d.success === false)" in _EMAIL_LIBRARY
    assert "d.error || d.detail || 'Failed to save contact'" in _EMAIL_LIBRARY


@pytest.mark.asyncio
async def test_add_contact_validation_is_http_400(contact_routes):
    status, body = _payload(await contact_routes["add"]({"name": "Ada", "email": ""}, _admin="admin"))
    assert status == 400
    assert body == {"success": False, "error": "Email required"}


@pytest.mark.asyncio
async def test_add_contact_write_failure_is_http_502(contact_routes, monkeypatch):
    monkeypatch.setattr(contact_routes["module"], "_create_contact", lambda *a, **k: False)
    status, body = _payload(await contact_routes["add"](
        {"name": "Ada", "email": "ada@example.com"},
        _admin="admin",
    ))
    assert status == 502
    assert body == {"success": False, "error": "Failed to save contact"}


@pytest.mark.asyncio
async def test_add_contact_success_still_returns_200(contact_routes, monkeypatch):
    created = []
    monkeypatch.setattr(
        contact_routes["module"],
        "_create_contact",
        lambda name, email: created.append((name, email)) or True,
    )
    status, body = _payload(await contact_routes["add"](
        {"name": "Ada", "email": "ada@example.com"},
        _admin="admin",
    ))
    assert status == 200
    assert body == {"success": True}
    assert created == [("Ada", "ada@example.com")]


@pytest.mark.asyncio
async def test_add_contact_already_exists_still_returns_200(contact_routes, monkeypatch):
    existing = {"uid": "1", "name": "Ada", "emails": ["ada@example.com"]}
    monkeypatch.setattr(contact_routes["module"], "_fetch_contacts", lambda *a, **k: [existing])
    status, body = _payload(await contact_routes["add"](
        {"name": "Ada Lovelace", "email": "ada@example.com"},
        _admin="admin",
    ))
    assert status == 200
    assert body["success"] is True
    assert body["message"] == "Already exists"


@pytest.mark.asyncio
async def test_edit_contact_validation_is_http_400(contact_routes):
    status, body = _payload(await contact_routes["edit"](
        "uid-1",
        {"name": "  ", "emails": []},
        _admin="admin",
    ))
    assert status == 400
    assert body == {"success": False, "error": "Name or email required"}


@pytest.mark.asyncio
async def test_edit_contact_write_failure_is_http_502(contact_routes, monkeypatch):
    monkeypatch.setattr(contact_routes["module"], "_update_contact", lambda *a, **k: False)
    status, body = _payload(await contact_routes["edit"](
        "uid-1",
        {"name": "Ada", "emails": ["ada@example.com"]},
        _admin="admin",
    ))
    assert status == 502
    assert body == {"success": False, "error": "Failed to update contact"}


@pytest.mark.asyncio
async def test_delete_contact_write_failure_is_http_502(contact_routes, monkeypatch):
    monkeypatch.setattr(contact_routes["module"], "_delete_contact", lambda *a, **k: False)
    status, body = _payload(await contact_routes["delete"]("uid-1", _admin="admin"))
    assert status == 502
    assert body == {"success": False, "error": "Failed to delete contact"}


@pytest.mark.asyncio
async def test_import_missing_payload_is_http_400(contact_routes):
    status, body = _payload(await contact_routes["import"]({}, _admin="admin"))
    assert status == 400
    assert body == {"success": False, "error": "No contact data found"}


@pytest.mark.asyncio
async def test_import_all_failed_is_http_502(contact_routes, monkeypatch):
    monkeypatch.setattr(
        contact_routes["module"],
        "_import_vcards",
        lambda text: {"imported": 0, "failed": 3, "total": 3},
    )
    status, body = _payload(await contact_routes["import"](
        {"vcf": "BEGIN:VCARD\nFN:Ada\nEND:VCARD"},
        _admin="admin",
    ))
    assert status == 502
    assert body["success"] is False
    assert body["failed"] == 3
    assert body["error"] == "Failed to import contacts"
