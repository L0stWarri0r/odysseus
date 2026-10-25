"""AI document tidy must not return exception text.

POST /api/documents/ai-tidy used to raise HTTP 500 with "AI tidy failed:
{exception}". A failed commit can include SQL and bound values in that
message, and a model response that is not valid JSON can include the
document preview. An unexpected failure now returns HTTP 502 with a fixed
sentence. The exception is logged and is not returned. A rollback failure,
including one raised again while closing the session, still returns that
same 502. An HTTPException raised inside the handler is not rewritten:
"No endpoint configured for AI tidy" and "AI returned invalid response"
stay HTTP 500. Another user's documents stay in place. An anonymous caller
still matches no rows and deletes nothing. Success still removes that
caller's junk document, marks a keeper, and leaves another owner's rows
alone.

The library Tidy button reads the AI body only when res.ok is true. A
non-ok AI response, when nothing was removed or renamed, shows the same
fixed sentence and returns before "Already tidy". It does not toast
detail or the exception object.
"""
import json
import logging
import os
import re
import uuid
from pathlib import Path

os.makedirs("data", exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.document_routes as document_routes
from core.database import Document, SessionLocal

_COMMIT_SECRET = "COMMIT_SECRET sql=DELETE FROM documents title=Scratch token=ai-tidy"
_ROLLBACK_SECRET = "ROLLBACK_SECRET connection reset while rolling back ai tidy"
_PARSE_SECRET = "PARSE_SECRET preview=do-not-return-this-document-body"
# The route looks up SessionLocal on this module at call time. Another test
# replaces that name with a temp engine; reads and cleanup use the app engine.
_REAL_SESSION = SessionLocal
_REPO = Path(__file__).resolve().parent.parent

_KEEP_BODY = (
    "# Field report\n\n"
    "The north platform was clear, and the evening train left on time with the samples."
)
_JUNK_BODY = "test test test"


class _FailingSession:
    """Session whose commit or rollback raises, and whose close rolls back."""

    def __init__(self, inner, commit_exc=None, rollback_exc=None):
        self._inner = inner
        self._commit_exc = commit_exc
        self._rollback_exc = rollback_exc

    def commit(self):
        if self._commit_exc is not None:
            raise self._commit_exc
        return self._inner.commit()

    def rollback(self):
        if self._rollback_exc is not None:
            raise self._rollback_exc
        return self._inner.rollback()

    def close(self):
        try:
            self.rollback()
        finally:
            self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _client(username):
    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        if username is not None:
            request.state.current_user = username
        return await call_next(request)

    app.include_router(document_routes.setup_document_routes(None, None))
    return TestClient(app)


def _real_session():
    return _REAL_SESSION()


def _pin_session(monkeypatch):
    monkeypatch.setattr(document_routes, "SessionLocal", _REAL_SESSION)


def _install_failing_session(monkeypatch, commit_exc=None, rollback_exc=None):
    def _factory():
        return _FailingSession(
            _REAL_SESSION(),
            commit_exc=commit_exc,
            rollback_exc=rollback_exc,
        )

    monkeypatch.setattr(document_routes, "SessionLocal", _factory)


def _install_endpoint(monkeypatch, llm):
    monkeypatch.setattr(
        "src.task_endpoint.resolve_task_endpoint",
        lambda *args, **kwargs: ("http://llm.test/v1", "tidy-model", {}),
    )
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *args, **kwargs: ("http://llm.test/v1", "tidy-model", {}),
    )
    monkeypatch.setattr("src.llm_core.llm_call_async", llm)


def _install_no_endpoint(monkeypatch):
    monkeypatch.setattr(
        "src.task_endpoint.resolve_task_endpoint",
        lambda *args, **kwargs: (None, None, None),
    )
    monkeypatch.setattr(
        "src.endpoint_resolver.resolve_endpoint",
        lambda *args, **kwargs: ("", "", {}),
    )


def _add_document(owner, title, content, is_active=True, archived=False, tidy_verdict=None):
    db = _real_session()
    try:
        doc_id = str(uuid.uuid4())
        db.add(Document(
            id=doc_id,
            session_id=None,
            title=title,
            language="markdown",
            current_content=content,
            version_count=1,
            is_active=is_active,
            archived=archived,
            owner=owner,
            tidy_verdict=tidy_verdict,
        ))
        db.commit()
        return doc_id
    finally:
        db.close()


def _delete(doc_ids):
    db = _real_session()
    try:
        if doc_ids:
            db.query(Document).filter(Document.id.in_(doc_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _row(doc_id):
    db = _real_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc is None:
            return None
        return {
            "title": doc.title,
            "current_content": doc.current_content,
            "is_active": doc.is_active,
            "owner": doc.owner,
            "archived": bool(doc.archived),
            "tidy_verdict": doc.tidy_verdict,
        }
    finally:
        db.close()


def _js(path, start_marker, end_marker):
    source = (_REPO / path).read_text(encoding="utf-8")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


async def _junk_keep_llm(url, model, messages, **kwargs):
    prompt = messages[-1]["content"]
    titles = re.findall(r'title="([^"]*)"', prompt)
    verdicts = ["junk" if title == "Scratch" else "keep" for title in titles]
    return json.dumps(verdicts)


async def _invalid_llm(url, model, messages, **kwargs):
    return f"no array here {_PARSE_SECRET}"


async def _bad_json_llm(url, model, messages, **kwargs):
    return f"[{_PARSE_SECRET}]"


async def _unused_llm(url, model, messages, **kwargs):
    raise AssertionError("LLM should not be called")


def test_ai_tidy_commit_failure_hides_exception(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    junk_id = _add_document(owner, "Scratch", _JUNK_BODY)
    _install_endpoint(monkeypatch, _junk_keep_llm)
    _install_failing_session(monkeypatch, commit_exc=RuntimeError(_COMMIT_SECRET))
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post("/api/documents/ai-tidy")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to tidy documents with AI"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert "AI tidy failed:" not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert "Failed to tidy documents with AI" in caplog.text
        assert _row(junk_id)["title"] == "Scratch"
        assert _row(junk_id)["current_content"] == _JUNK_BODY
        assert _row(junk_id)["tidy_verdict"] is None
    finally:
        _delete([junk_id])


def test_ai_tidy_rollback_failure_stays_fixed_502(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    junk_id = _add_document(owner, "Scratch", _JUNK_BODY)
    _install_endpoint(monkeypatch, _junk_keep_llm)
    _install_failing_session(
        monkeypatch,
        commit_exc=RuntimeError(_COMMIT_SECRET),
        rollback_exc=RuntimeError(_ROLLBACK_SECRET),
    )
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post("/api/documents/ai-tidy")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to tidy documents with AI"}
        assert "detail" not in resp.json()
        assert _COMMIT_SECRET not in resp.text
        assert _ROLLBACK_SECRET not in resp.text
        assert _COMMIT_SECRET in caplog.text
        assert _ROLLBACK_SECRET in caplog.text
        assert "AI tidy rollback failed" in caplog.text
        assert "AI tidy session close failed" in caplog.text
        assert _row(junk_id)["current_content"] == _JUNK_BODY
        assert _row(junk_id)["owner"] == owner
        assert _row(junk_id)["tidy_verdict"] is None
    finally:
        _delete([junk_id])


def test_ai_tidy_bad_json_hides_response_text(monkeypatch, caplog):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    junk_id = _add_document(owner, "Scratch", _JUNK_BODY)
    _install_endpoint(monkeypatch, _bad_json_llm)
    caplog.set_level(logging.ERROR, logger="routes.document_routes")
    try:
        resp = _client(owner).post("/api/documents/ai-tidy")
        assert resp.status_code == 502
        assert resp.json() == {"error": "Failed to tidy documents with AI"}
        assert "detail" not in resp.json()
        assert _PARSE_SECRET not in resp.text
        assert "AI tidy failed:" not in resp.text
        assert "JSONDecodeError" in caplog.text
        assert "Failed to tidy documents with AI" in caplog.text
        assert _row(junk_id)["tidy_verdict"] is None
        assert _row(junk_id)["current_content"] == _JUNK_BODY
    finally:
        _delete([junk_id])


def test_ai_tidy_invalid_response_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    junk_id = _add_document(owner, "Scratch", _JUNK_BODY)
    _install_endpoint(monkeypatch, _invalid_llm)
    try:
        resp = _client(owner).post("/api/documents/ai-tidy")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "AI returned invalid response"}
        assert resp.json().get("error") != "Failed to tidy documents with AI"
        assert _PARSE_SECRET not in resp.text
        assert _row(junk_id)["title"] == "Scratch"
        assert _row(junk_id)["tidy_verdict"] is None
    finally:
        _delete([junk_id])


def test_ai_tidy_missing_endpoint_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    junk_id = _add_document(owner, "Scratch", _JUNK_BODY)
    _install_no_endpoint(monkeypatch)
    try:
        resp = _client(owner).post("/api/documents/ai-tidy")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "No endpoint configured for AI tidy"}
        assert resp.json().get("error") != "Failed to tidy documents with AI"
        assert _row(junk_id)["tidy_verdict"] is None
        assert _row(junk_id)["current_content"] == _JUNK_BODY
    finally:
        _delete([junk_id])


def test_ai_tidy_http_exception_inside_handler_is_not_rewritten(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    junk_id = _add_document(owner, "Scratch", _JUNK_BODY)
    _install_endpoint(monkeypatch, _unused_llm)

    def _reject(*args, **kwargs):
        raise HTTPException(400, "AI tidy rejected")

    monkeypatch.setattr(document_routes, "_owner_session_filter", _reject)
    try:
        resp = _client(owner).post("/api/documents/ai-tidy")
        assert resp.status_code == 400
        assert resp.json() == {"detail": "AI tidy rejected"}
        assert resp.json().get("error") != "Failed to tidy documents with AI"
        assert _row(junk_id)["title"] == "Scratch"
        assert _row(junk_id)["tidy_verdict"] is None
    finally:
        _delete([junk_id])


def test_ai_tidy_anonymous_deletes_nothing(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    junk_id = _add_document(owner, "Scratch", _JUNK_BODY)
    _install_endpoint(monkeypatch, _unused_llm)
    try:
        resp = _client(None).post("/api/documents/ai-tidy")
        assert resp.status_code == 200
        assert resp.json() == {
            "deleted": 0,
            "reviewed": 0,
            "message": "All documents already reviewed",
        }
        assert "error" not in resp.json()
        assert _row(junk_id)["owner"] == owner
        assert _row(junk_id)["tidy_verdict"] is None
        assert _row(junk_id)["current_content"] == _JUNK_BODY
    finally:
        _delete([junk_id])


def test_ai_tidy_success_removes_junk_and_skips_other_owner(monkeypatch):
    _pin_session(monkeypatch)
    owner = f"health-{uuid.uuid4().hex}"
    other = f"health-{uuid.uuid4().hex}"
    junk_id = _add_document(owner, "Scratch", _JUNK_BODY)
    keep_id = _add_document(owner, "Field report", _KEEP_BODY)
    reviewed_id = _add_document(owner, "Already reviewed", _KEEP_BODY, tidy_verdict="keep")
    archived_id = _add_document(owner, "Archived scratch", _JUNK_BODY, archived=True)
    inactive_id = _add_document(owner, "Inactive scratch", _JUNK_BODY, is_active=False)
    other_id = _add_document(other, "Scratch", _JUNK_BODY)
    ids = [junk_id, keep_id, reviewed_id, archived_id, inactive_id, other_id]
    _install_endpoint(monkeypatch, _junk_keep_llm)
    try:
        resp = _client(owner).post("/api/documents/ai-tidy")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "deleted": 1,
            "reviewed": 2,
            "remaining": 0,
            "message": "Reviewed 2, removed 1 junk document",
        }
        assert "error" not in body
        assert _row(junk_id) is None
        assert _row(keep_id)["tidy_verdict"] == "keep"
        assert _row(keep_id)["current_content"] == _KEEP_BODY
        assert _row(keep_id)["owner"] == owner
        assert _row(reviewed_id)["tidy_verdict"] == "keep"
        assert _row(reviewed_id)["title"] == "Already reviewed"
        assert _row(archived_id)["tidy_verdict"] is None
        assert _row(archived_id)["archived"] is True
        assert _row(inactive_id)["is_active"] is False
        assert _row(inactive_id)["tidy_verdict"] is None
        assert _row(other_id)["owner"] == other
        assert _row(other_id)["tidy_verdict"] is None
        assert _row(other_id)["current_content"] == _JUNK_BODY
    finally:
        _delete(ids)


def test_ai_tidy_handler_hides_exception_text():
    source = (_REPO / "routes/document_routes.py").read_text(encoding="utf-8")
    start = source.index("async def ai_tidy_documents")
    end = source.index("async def export_pdf_preview", start)
    block = source[start:end]
    assert "except HTTPException:" in block
    assert 'document_error("Failed to tidy documents with AI")' in block
    assert 'logger.exception("Failed to tidy documents with AI")' in block
    assert "AI tidy rollback failed" in block
    assert "AI tidy session close failed" in block
    assert "str(e)" not in block
    assert "AI tidy failed:" not in block
    assert 'HTTPException(500, "No endpoint configured for AI tidy")' in block
    assert 'HTTPException(500, "AI returned invalid response")' in block


def test_ai_tidy_button_does_not_claim_already_tidy_on_failure():
    block = _js(
        "static/js/documentLibrary.js",
        "const tidyBtn = document.getElementById('doclib-tidy-btn');",
        "// Select mode",
    )
    not_ok = block.index("if (!res2.ok)")
    json_at = block.index("await res2.json()")
    toast_at = block.index("showToast('Failed to tidy documents with AI')")
    return_at = block.index("return;", toast_at)
    already_at = block.index("Already tidy")
    assert not_ok < json_at < toast_at < return_at < already_at
    assert "aiFailed && totalDeleted === 0 && totalFixed === 0" in block
    assert "console.error('Failed to tidy documents with AI')" in block
    assert block.count("showToast('Failed to tidy documents with AI')") == 1
    assert block.count("console.error('Failed to tidy documents with AI')") == 1
    assert "detail" not in block
    assert "e.message" not in block
