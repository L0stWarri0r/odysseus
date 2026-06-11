from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_static_js(name: str) -> str:
    return (ROOT / "static" / "js" / name).read_text(encoding="utf-8")


def test_group_chat_role_label_is_escaped_before_html_insertion():
    source = _read_static_js("group.js")

    assert "uiModule.esc(roleLabel)" in source
    assert '<div class="role">${roleLabel}' not in source


def test_model_picker_display_name_is_escaped_when_logo_uses_inner_html():
    source = _read_static_js("modelPicker.js")

    assert "uiModule.esc(displayName)" in source
    assert "'</span> ' + displayName" not in source
