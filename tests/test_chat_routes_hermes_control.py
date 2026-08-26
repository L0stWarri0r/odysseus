import pytest
from fastapi import HTTPException

from src.hermes_control.chat_integration import _apply_hermes_control_policy


class DummySession:
    def __init__(self, endpoint_url, model="dummy-model"):
        self.endpoint_url = endpoint_url
        self.model = model


def test_chat_policy_disables_web_for_local_model_before_context_build():
    result = _apply_hermes_control_policy(
        message="Search the web for local LLM news",
        session_id="s-local",
        sess=DummySession("http://localhost:1234/v1"),
        mode="chat",
        private_mode=False,
        use_web="true",
        use_research="false",
        allow_web_search="false",
    )

    assert result.use_web is False
    assert result.use_research is False
    assert result.allow_web_search is False
    assert result.policy.decision == "allow_with_adjustments"
    assert "disable_web" in result.policy.actions


def test_chat_policy_private_local_mode_does_not_inspect_or_block_prompt_text():
    result = _apply_hermes_control_policy(
        message="OPENAI_API_KEY=sk-testsecret12345 C:\\Users\\Chase\\Documents\\private.txt",
        session_id="s-private",
        sess=DummySession("http://127.0.0.1:1234/v1"),
        mode="chat",
        private_mode=True,
        use_web="true",
        use_research="true",
        allow_web_search="true",
    )

    assert result.policy.content_visible_to_hermes is False
    assert result.policy.findings == []
    assert result.use_web is False
    assert result.use_research is False
    assert result.allow_web_search is False


def test_chat_policy_incognito_implies_private_mode_on_local_endpoint():
    result = _apply_hermes_control_policy(
        message="OPENAI_API_KEY=sk-testsecret12345 C:\\Users\\Chase\\Documents\\private.txt",
        session_id="s-nobody",
        sess=DummySession("http://127.0.0.1:1234/v1"),
        mode="chat",
        private_mode=False,
        incognito=True,
        use_web="true",
        use_research="true",
        allow_web_search="true",
    )

    assert result.policy.content_visible_to_hermes is False
    assert result.policy.findings == []
    assert result.use_web is False
    assert result.use_research is False
    assert result.allow_web_search is False


def test_chat_policy_blocks_visible_secret_before_llm_work():
    with pytest.raises(HTTPException) as excinfo:
        _apply_hermes_control_policy(
            message="OPENAI_API_KEY=sk-testsecret12345",
            session_id="s-cloud",
            sess=DummySession("https://api.openai.com/v1"),
            mode="chat",
            private_mode=False,
            use_web="false",
            use_research="false",
            allow_web_search="false",
        )

    assert excinfo.value.status_code == 400
    assert "secret" in str(excinfo.value.detail).lower()
