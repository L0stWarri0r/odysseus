from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from routes.task_routes import _require_task_owner
from routes.tts_routes import MAX_TTS_TEXT_LENGTH, TTSRequest


def test_task_owner_gate_rejects_owned_task_without_user():
    with pytest.raises(HTTPException) as exc:
        _require_task_owner(SimpleNamespace(owner="alice"), None)

    assert exc.value.status_code == 404


def test_task_owner_gate_rejects_null_owner_for_authenticated_user():
    with pytest.raises(HTTPException) as exc:
        _require_task_owner(SimpleNamespace(owner=None), "alice")

    assert exc.value.status_code == 404


def test_task_owner_gate_accepts_exact_owner_and_unowned_anonymous_task():
    _require_task_owner(SimpleNamespace(owner="alice"), "alice")
    _require_task_owner(SimpleNamespace(owner=None), None)
    _require_task_owner(SimpleNamespace(owner=""), None)


def test_tts_request_rejects_oversized_text():
    with pytest.raises(ValidationError):
        TTSRequest(text="x" * (MAX_TTS_TEXT_LENGTH + 1))


def test_tts_request_rejects_unknown_format():
    with pytest.raises(ValidationError):
        TTSRequest(text="hello", format="json")


def test_generated_image_route_uses_fail_closed_auth_check():
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    start = src.index('@app.get("/api/generated-image/{filename}")')
    end = src.index("# ========= YOUTUBE INIT", start)
    block = src[start:end]

    assert "require_user(request)" in block
    assert "except Exception" not in block
    assert "_row is not None" in block


def test_sync_chat_session_lookup_does_not_mask_runtime_errors():
    src = (Path(__file__).resolve().parents[1] / "routes" / "webhook_routes.py").read_text(encoding="utf-8")
    start = src.index("# --- Case 1: Resume an existing session ---")
    end = src.index("# --- Case 2: Direct API key", start)
    block = src[start:end]

    assert "except KeyError:" in block
    assert "except (KeyError, Exception)" not in block
