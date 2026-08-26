import pytest

from src.hermes_control.models import HermesDecision, HermesRequestContext
from src.hermes_control.policy import evaluate


def test_private_local_mode_keeps_content_opaque_even_if_text_contains_sensitive_patterns():
    ctx = HermesRequestContext(
        message="OPENAI_API_KEY=sk-test-should-not-be-inspected C:\\Users\\Chase\\Documents\\private.txt",
        session_id="session-private",
        endpoint_url="http://127.0.0.1:1234/v1",
        model="local-model",
        private_mode=True,
        use_web=True,
        use_research=True,
    )

    result = evaluate(ctx)

    assert result.content_visible_to_hermes is False
    assert result.decision == HermesDecision.ALLOW_WITH_ADJUSTMENTS
    assert "disable_web" in result.actions
    assert "disable_research" in result.actions
    assert result.adjusted_context["use_web"] is False
    assert result.adjusted_context["use_research"] is False
    assert result.findings == []
    assert "OPENAI_API_KEY" not in result.reason
    assert "C:\\Users" not in result.reason


def test_non_private_secret_is_blocked():
    ctx = HermesRequestContext(
        message="Here is OPENAI_API_KEY=sk-testabcdefghijklmnopqrstuvwxyz123456",
        session_id="session-standard",
        endpoint_url="https://api.openai.com/v1",
        model="cloud-model",
    )

    result = evaluate(ctx)

    assert result.content_visible_to_hermes is True
    assert result.decision == HermesDecision.BLOCK
    assert any(f.type == "secret" for f in result.findings)
    assert "secret" in result.reason.lower()


def test_local_path_alone_is_allowed_not_nagged():
    ctx = HermesRequestContext(
        message="Open C:\\Users\\Chase\\Projects\\odysseus and explain the structure.",
        session_id="session-standard",
        endpoint_url="https://api.openai.com/v1",
        model="cloud-model",
    )

    result = evaluate(ctx)

    assert result.decision == HermesDecision.ALLOW
    assert any(f.type == "local_path" and f.severity == "info" for f in result.findings)
    assert result.requires_user_permission is False


def test_unix_local_path_is_allowed_not_nagged():
    ctx = HermesRequestContext(
        message="Open /home/lost/Projects/odysseus and explain the structure.",
        session_id="session-unix",
        endpoint_url="https://api.openai.com/v1",
        model="cloud-model",
    )

    result = evaluate(ctx)

    assert result.decision == HermesDecision.ALLOW
    assert any(f.type == "local_path" and f.severity == "info" for f in result.findings)
    assert result.requires_user_permission is False


def test_url_with_home_segment_is_not_a_local_path_finding():
    ctx = HermesRequestContext(
        message="Read https://example.com/home/lost/docs for the API.",
        session_id="session-url",
        endpoint_url="https://api.openai.com/v1",
        model="cloud-model",
    )

    result = evaluate(ctx)

    assert result.decision == HermesDecision.ALLOW
    assert not any(f.type == "local_path" for f in result.findings)


def test_bare_provider_api_key_is_blocked():
    ctx = HermesRequestContext(
        message="Use sk-proj-abcdefghijklmnopqrstuvwxyz1234567890 please.",
        session_id="session-key",
        endpoint_url="https://api.openai.com/v1",
        model="cloud-model",
    )

    result = evaluate(ctx)

    assert result.decision == HermesDecision.BLOCK
    assert any(f.type == "secret" and f.label == "Provider API key" for f in result.findings)
    assert all(f.preview == "[REDACTED]" for f in result.findings if f.type == "secret")


def test_local_model_web_access_is_disabled():
    ctx = HermesRequestContext(
        message="Search the web for recent local LLM news.",
        session_id="session-local",
        endpoint_url="http://localhost:1234/v1",
        model="local-model",
        use_web=True,
    )

    result = evaluate(ctx)

    assert result.decision == HermesDecision.ALLOW_WITH_ADJUSTMENTS
    assert result.adjusted_context["use_web"] is False
    assert "disable_web" in result.actions
    assert result.requires_user_permission is False
