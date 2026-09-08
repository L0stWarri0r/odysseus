"""Health assessment 2026-09-08: scheduled-mail and folder failures must not look like success."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse


_REPO = Path(__file__).resolve().parents[1]
_EMAIL_HELPERS = (_REPO / "routes" / "email_helpers.py").read_text(encoding="utf-8")
_EMAIL_ROUTES = (_REPO / "routes" / "email_routes.py").read_text(encoding="utf-8")
_EMAIL_LIBRARY = (_REPO / "static" / "js" / "emailLibrary.js").read_text(encoding="utf-8")
_EMAIL_INBOX = (_REPO / "static" / "js" / "emailInbox.js").read_text(encoding="utf-8")


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
def scheduled_routes(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()
    router = email_routes.setup_email_routes()
    return {
        "schedule": _route_endpoint(router, "/api/email/schedule", "POST"),
        "list": _route_endpoint(router, "/api/email/scheduled", "GET"),
        "cancel": _route_endpoint(router, "/api/email/scheduled/{sid}", "DELETE"),
        "folders": _route_endpoint(router, "/api/email/folders", "GET"),
        "db_path": db_path,
        "email_routes": email_routes,
    }


def test_mail_error_helper_returns_json_error_with_non_200_status():
    assert "def mail_error(message: str, status_code: int = 502)" in _EMAIL_HELPERS
    resp = JSONResponse({"success": False, "error": "Mail operation failed"}, status_code=502)
    assert resp.status_code == 502
    compact = resp.body.replace(b" ", b"")
    assert b'"success":false' in compact or b'"success": false' in resp.body


def test_schedule_folder_and_cancel_routes_use_mail_error():
    assert "return mail_error(\"send_at required (ISO8601 UTC)\", 400)" in _EMAIL_ROUTES
    assert "return mail_error(\"send_at must be in the future\", 400)" in _EMAIL_ROUTES
    assert "now_utc - _td(seconds=30)" in _EMAIL_ROUTES
    assert "return mail_error(\"Scheduled email not found\", 404)" in _EMAIL_ROUTES
    assert "list_folders failed" in _EMAIL_ROUTES
    assert "list_scheduled failed" in _EMAIL_ROUTES
    assert 'return mail_error("Mail operation failed")' in _EMAIL_ROUTES
    assert "except HTTPException:\n            raise" in _EMAIL_ROUTES


def test_library_and_inbox_do_not_treat_folder_or_schedule_errors_as_empty_success():
    assert "if (!res.ok || data.error) return;" in _EMAIL_LIBRARY
    assert "Failed to load scheduled emails" in _EMAIL_LIBRARY
    assert "data.success === false" in _EMAIL_LIBRARY
    assert "Failed to cancel scheduled email" in _EMAIL_LIBRARY
    assert "if (!res.ok || data.error)" in _EMAIL_INBOX
    assert "Failed to load folders:" in _EMAIL_INBOX


@pytest.mark.asyncio
async def test_schedule_allows_send_at_within_30s_grace(scheduled_routes):
    endpoint = scheduled_routes["schedule"]
    send_at = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
    status, body = _payload(await endpoint(
        {"to": "a@example.com", "body": "b", "send_at": send_at},
        owner="alice",
    ))
    assert status == 200
    assert body["success"] is True
    assert body["id"]


@pytest.mark.asyncio
async def test_schedule_rejects_send_at_older_than_grace(scheduled_routes):
    endpoint = scheduled_routes["schedule"]
    send_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    status, body = _payload(await endpoint(
        {"to": "a@example.com", "body": "b", "send_at": send_at},
        owner="alice",
    ))
    assert status == 400
    assert body["success"] is False
    assert body["error"] == "send_at must be in the future"


@pytest.mark.asyncio
async def test_schedule_missing_send_at_is_http_400(scheduled_routes):
    status, body = _payload(await scheduled_routes["schedule"](
        {"to": "a@example.com", "body": "b"},
        owner="alice",
    ))
    assert status == 400
    assert body == {"success": False, "error": "send_at required (ISO8601 UTC)"}


@pytest.mark.asyncio
async def test_schedule_does_not_swallow_account_ownership_http_errors(scheduled_routes, monkeypatch):
    def boom(account_id, owner):
        raise HTTPException(404, "Account not found")

    monkeypatch.setattr(scheduled_routes["email_routes"], "_assert_owns_account", boom)
    send_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with pytest.raises(HTTPException) as exc:
        await scheduled_routes["schedule"](
            {
                "to": "a@example.com",
                "body": "b",
                "send_at": send_at,
                "account_id": "acct-bob",
            },
            owner="alice",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_unknown_scheduled_email_is_http_404(scheduled_routes):
    status, body = _payload(await scheduled_routes["cancel"]("missing", owner="alice"))
    assert status == 404
    assert body == {"success": False, "error": "Scheduled email not found"}


@pytest.mark.asyncio
async def test_cancel_other_users_scheduled_email_is_http_404(scheduled_routes):
    send_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    created = await scheduled_routes["schedule"](
        {"to": "b@example.com", "body": "bob", "send_at": send_at},
        owner="bob",
    )
    status, body = _payload(await scheduled_routes["cancel"](created["id"], owner="alice"))
    assert status == 404
    assert body["success"] is False
    listed = await scheduled_routes["list"](owner="bob")
    assert [row["id"] for row in listed["scheduled"]] == [created["id"]]


@pytest.mark.asyncio
async def test_list_scheduled_db_failure_is_not_empty_success(scheduled_routes, tmp_path, monkeypatch):
    missing = tmp_path / "missing-dir" / "scheduled.db"
    monkeypatch.setattr(scheduled_routes["email_routes"], "SCHEDULED_DB", missing)
    status, body = _payload(await scheduled_routes["list"](owner="alice"))
    assert status == 502
    assert body["success"] is False
    assert body["error"] == "Mail operation failed"
    assert "scheduled" not in body


@pytest.mark.asyncio
async def test_folder_list_failure_is_not_empty_folder_success(scheduled_routes, monkeypatch):
    class BoomImap:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("imap down")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(scheduled_routes["email_routes"], "_imap", BoomImap)
    status, body = _payload(await scheduled_routes["folders"](account_id=None, owner="alice"))
    assert status == 502
    assert body["success"] is False
    assert body["error"] == "Mail operation failed"
    assert "folders" not in body
