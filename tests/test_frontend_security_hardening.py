from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_group_chat_role_label_is_escaped_before_inner_html():
    source = _read("static/js/group.js")

    assert "${uiModule.esc(roleLabel)}" in source
    assert "${roleLabel} <span class=\"role-timestamp\"" not in source


def test_mermaid_uses_strict_security_level():
    source = _read("static/js/markdown.js")

    assert "securityLevel: 'strict'" in source
    assert "securityLevel: 'loose'" not in source


def test_model_picker_escapes_display_name_in_logo_branch():
    source = _read("static/js/modelPicker.js")

    assert "'</span> ' + uiModule.esc(displayName)" in source
    assert "'</span> ' + displayName" not in source


def test_compare_tool_and_provider_labels_are_escaped():
    stream = _read("static/js/compare/stream.js")
    selector = _read("static/js/compare/selector.js")

    assert '<span class="agent-thread-tool">${escapeHtml(toolLabel)}</span>' in stream
    assert '<span class="agent-thread-tool">${toolLabel}</span>' not in stream
    assert '${escapeHtml(p.label || p.id)}' in selector
    assert '${p.label || p.id}' not in selector


def test_cookbook_state_redacts_huggingface_token():
    source = _read("static/js/cookbookRunning.js")

    assert "delete env.hfToken;" in source
    assert "env.hfToken = hfToken" not in source


def test_admin_mcp_error_and_modal_labels_are_escaped():
    admin = _read("static/js/admin.js")
    modal = _read("static/js/modalManager.js")

    assert "Error: ${esc(s.error || 'unknown')}" in admin
    assert "Error: ${s.error || 'unknown'}" not in admin
    assert "${_escapeHtml(meta.label)}" in modal
    assert "${meta.label}</span>" not in modal
