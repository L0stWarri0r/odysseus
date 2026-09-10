"""Health assessment 2026-09-10: send/draft/AI mail failures must not look like HTTP 200."""

import json
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from routes.email_helpers import ExtractStyleRequest, SendEmailRequest, mail_error


_REPO = Path(__file__).resolve().parents[1]
_EMAIL_HELPERS = (_REPO / "routes" / "email_helpers.py").read_text(encoding="utf-8")
_EMAIL_ROUTES = (_REPO / "routes" / "email_routes.py").read_text(encoding="utf-8")
_DOCUMENT_JS = (_REPO / "static" / "js" / "document.js").read_text(encoding="utf-8")
_EMAIL_INBOX = (_REPO / "static" / "js" / "emailInbox.js").read_text(encoding="utf-8")
_EMAIL_LIBRARY = (_REPO / "static" / "js" / "emailLibrary.js").read_text(encoding="utf-8")
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


class BoomImap:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("IMAP AUTH failed password=super-secret")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class EmptySentImap:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def select(self, *args, **kwargs):
        return ("OK", [b""])

    def search(self, *args, **kwargs):
        return ("OK", [b""])


@pytest.fixture
def compose_routes(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()
    router = email_routes.setup_email_routes()
    return {
        "send": _route_endpoint(router, "/api/email/send", "POST"),
        "draft": _route_endpoint(router, "/api/email/draft", "POST"),
        "extract": _route_endpoint(router, "/api/email/extract-style", "POST"),
        "summarize": _route_endpoint(router, "/api/email/summarize", "POST"),
        "ai_reply": _route_endpoint(router, "/api/email/ai-reply", "POST"),
        "email_routes": email_routes,
    }


def test_mail_error_helper_returns_json_error_with_non_200_status():
    assert "def mail_error(message: str, status_code: int = 502)" in _EMAIL_HELPERS
    resp = mail_error("Failed to send email")
    assert resp.status_code == 502
    assert json.loads(resp.body) == {"success": False, "error": "Failed to send email"}


def test_send_draft_and_ai_routes_use_mail_error():
    assert "return mail_error(str(e) or \"No SMTP-capable email account configured\", 400)" in _EMAIL_ROUTES
    assert "return mail_error(\"Failed to save draft\")" in _EMAIL_ROUTES
    assert "return mail_error(\"No body provided\", 400)" in _EMAIL_ROUTES
    assert "return mail_error(\"No email body provided\", 400)" in _EMAIL_ROUTES
    assert "return mail_error(\"No LLM endpoint configured\", 400)" in _EMAIL_ROUTES
    assert "return mail_error(\"Empty response from model\")" in _EMAIL_ROUTES
    assert "return mail_error(\"LLM returned empty response\")" in _EMAIL_ROUTES
    assert _EMAIL_ROUTES.count("except HTTPException:\n            raise") >= 4


def test_compose_ui_still_inspects_success_flag():
    assert "if (data.success)" in _DOCUMENT_JS
    assert "if (data.success && data.reply)" in _DOCUMENT_JS
    assert "if (result.success && result.reply)" in _EMAIL_INBOX
    assert "if (result.success && result.summary)" in _EMAIL_LIBRARY
    assert "if (data.success && data.style)" in _SETTINGS_JS


@pytest.mark.asyncio
async def test_send_missing_smtp_config_is_http_400(compose_routes, monkeypatch):
    monkeypatch.setattr(
        compose_routes["email_routes"],
        "_resolve_send_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("No SMTP-capable email account configured")),
    )
    status, body = _payload(await compose_routes["send"](
        SendEmailRequest(to="a@example.com", subject="Hi", body="Hello"),
        BackgroundTasks(),
        owner="alice",
    ))
    assert status == 400
    assert body == {"success": False, "error": "No SMTP-capable email account configured"}


@pytest.mark.asyncio
async def test_send_wait_for_delivery_smtp_failure_is_http_502(compose_routes, monkeypatch):
    routes = compose_routes["email_routes"]

    monkeypatch.setattr(routes, "_resolve_send_config", lambda *args, **kwargs: {
        "from_address": "alice@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_user": "alice",
        "smtp_password": "secret",
        "account_id": "acct-alice",
    })

    def boom(*args, **kwargs):
        raise RuntimeError("SMTP AUTH failed password=super-secret")

    monkeypatch.setattr(routes, "_send_smtp_message", boom)

    status, body = _payload(await compose_routes["send"](
        SendEmailRequest(
            to="a@example.com",
            subject="Hi",
            body="Hello",
            wait_for_delivery=True,
        ),
        BackgroundTasks(),
        owner="alice",
    ))
    assert status == 502
    assert body["success"] is False
    assert body["error"] == "Failed to send email"
    assert "super-secret" not in body["error"]


@pytest.mark.asyncio
async def test_send_does_not_swallow_account_ownership_http_errors(compose_routes, monkeypatch):
    def boom(account_id, owner):
        raise HTTPException(404, "Account not found")

    monkeypatch.setattr(compose_routes["email_routes"], "_assert_owns_account", boom)
    with pytest.raises(HTTPException) as exc:
        await compose_routes["send"](
            SendEmailRequest(
                to="a@example.com",
                subject="Hi",
                body="Hello",
                account_id="acct-bob",
            ),
            BackgroundTasks(),
            owner="alice",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_draft_imap_failure_is_http_502_without_leaking_exception(compose_routes, monkeypatch):
    routes = compose_routes["email_routes"]
    monkeypatch.setattr(routes, "_get_email_config", lambda *args, **kwargs: {
        "from_address": "alice@example.com",
    })
    monkeypatch.setattr(routes, "_imap", BoomImap)

    status, body = _payload(await compose_routes["draft"](
        SendEmailRequest(to="a@example.com", subject="Hi", body="Hello"),
        owner="alice",
    ))
    assert status == 502
    assert body == {"success": False, "error": "Failed to save draft"}
    assert "super-secret" not in json.dumps(body)


@pytest.mark.asyncio
async def test_summarize_missing_body_is_http_400(compose_routes):
    status, body = _payload(await compose_routes["summarize"]({}, owner="alice"))
    assert status == 400
    assert body == {"success": False, "error": "No body provided"}


@pytest.mark.asyncio
async def test_summarize_does_not_swallow_account_ownership_http_errors(compose_routes, monkeypatch):
    def boom(account_id, owner):
        raise HTTPException(404, "Account not found")

    monkeypatch.setattr(compose_routes["email_routes"], "_assert_owns_account", boom)
    with pytest.raises(HTTPException) as exc:
        await compose_routes["summarize"](
            {"body": "Please review the attached invoice", "account_id": "acct-bob"},
            owner="alice",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_ai_reply_missing_body_is_http_400(compose_routes):
    status, body = _payload(await compose_routes["ai_reply"]({"to": "a@example.com"}, owner="alice"))
    assert status == 400
    assert body == {"success": False, "error": "No email body provided"}


@pytest.mark.asyncio
async def test_extract_style_empty_sent_folder_is_http_404(compose_routes, monkeypatch):
    routes = compose_routes["email_routes"]
    monkeypatch.setattr(routes, "_imap", EmptySentImap)
    monkeypatch.setattr(routes, "_detect_sent_folder", lambda imap: "Sent")

    status, body = _payload(await compose_routes["extract"](
        ExtractStyleRequest(sample_count=15),
        owner="alice",
    ))
    assert status == 404
    assert body == {"success": False, "error": "No sent emails found"}


@pytest.mark.asyncio
async def test_extract_style_imap_failure_does_not_leak_exception(compose_routes, monkeypatch):
    monkeypatch.setattr(compose_routes["email_routes"], "_imap", BoomImap)
    status, body = _payload(await compose_routes["extract"](
        ExtractStyleRequest(sample_count=15),
        owner="alice",
    ))
    assert status == 502
    assert body == {"success": False, "error": "Mail operation failed"}
    assert "super-secret" not in json.dumps(body)
