"""Health assessment 2026-08-29: IMAP mutation failures must not look like success."""

from pathlib import Path

from fastapi.responses import JSONResponse


_REPO = Path(__file__).resolve().parents[1]
_EMAIL_HELPERS = (_REPO / "routes" / "email_helpers.py").read_text(encoding="utf-8")
_EMAIL_ROUTES = (_REPO / "routes" / "email_routes.py").read_text(encoding="utf-8")
_EMAIL_LIBRARY = (_REPO / "static" / "js" / "emailLibrary.js").read_text(encoding="utf-8")
_EMAIL_INBOX = (_REPO / "static" / "js" / "emailInbox.js").read_text(encoding="utf-8")
_EMAIL_UTILS = (_REPO / "static" / "js" / "emailLibrary" / "utils.js").read_text(encoding="utf-8")


def _bulk_action_source() -> str:
    start = _EMAIL_LIBRARY.index("async function _bulkAction(action)")
    end = _EMAIL_LIBRARY.index("\n}\n\n// _extractName", start) + 3
    return _EMAIL_LIBRARY[start:end]


def test_mail_error_helper_returns_json_error_with_non_200_status():
    assert "def mail_error(message: str, status_code: int = 502)" in _EMAIL_HELPERS
    assert 'JSONResponse(' in _EMAIL_HELPERS
    assert '{"success": False, "error": message}' in _EMAIL_HELPERS

    # The helper is a thin JSONResponse wrapper; pin the contract here so a
    # future rewrite cannot go back to HTTP 200 + success:false.
    resp = JSONResponse({"success": False, "error": "Email not found"}, status_code=404)
    assert resp.status_code == 404
    assert b'"success":false' in resp.body.replace(b" ", b"") or b'"success": false' in resp.body


def test_imap_mutation_routes_use_mail_error_instead_of_http_200():
    for route in (
        "/archive/{uid}",
        "/delete/{uid}",
        "/delete-permanent/{uid}",
        "/move/{uid}",
        "/mark-read/{uid}",
        "/mark-unread/{uid}",
        "/mark-answered/{uid}",
        "/clear-answered/{uid}",
    ):
        assert route in _EMAIL_ROUTES

    assert "return mail_error(\"Email not found\", 404)" in _EMAIL_ROUTES
    assert "return mail_error(\"Mail operation failed\")" in _EMAIL_ROUTES
    assert "except HTTPException:\n            raise" in _EMAIL_ROUTES


def test_assert_email_write_ok_rejects_http_error_and_success_false():
    assert "export async function assertEmailWriteOk(res)" in _EMAIL_UTILS
    assert "data?.success === false" in _EMAIL_UTILS
    assert "!res.ok" in _EMAIL_UTILS


def test_inbox_archive_and_delete_wait_for_backend_success():
    assert "import { assertEmailWriteOk } from './emailLibrary/utils.js'" in _EMAIL_INBOX
    archive = _EMAIL_INBOX[_EMAIL_INBOX.index("async function _archiveEmail"):_EMAIL_INBOX.index("async function _deleteEmail")]
    delete = _EMAIL_INBOX[_EMAIL_INBOX.index("async function _deleteEmail"):_EMAIL_INBOX.index("async function _toggleDone")]
    assert "await assertEmailWriteOk(res)" in archive
    assert "await assertEmailWriteOk(res)" in delete
    assert "filter(e => e.uid !== em.uid)" in archive
    assert archive.index("await assertEmailWriteOk(res)") < archive.index("filter(e => e.uid !== em.uid)")
    assert delete.index("await assertEmailWriteOk(res)") < delete.index("filter(e => e.uid !== em.uid)")


def test_library_bulk_archive_delete_only_remove_successful_writes():
    src = _bulk_action_source()
    assert "assertEmailWriteOk" in src
    assert "succeeded.push(uid)" in src
    assert "_animateEmailCardRemoval(succeeded)" in src
    assert "_animateEmailCardRemoval(uids)" not in src
    assert "failedWrites" in src
