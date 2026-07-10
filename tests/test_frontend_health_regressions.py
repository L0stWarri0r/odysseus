from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _source(*parts):
    return (ROOT / "static" / "js" / Path(*parts)).read_text(encoding="utf-8")


def test_generated_image_route_requires_user_and_does_not_fail_open():
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    handler = source.split("async def serve_generated_image", 1)[1].split("# ========= YOUTUBE INIT", 1)[0]
    assert "require_user(request)" in handler
    assert "Image authorization unavailable" in handler
    assert "except Exception:\n        pass" not in handler


def test_group_chat_role_label_is_escaped():
    source = _source("group.js")

    assert "${roleLabel}" not in source
    assert "uiModule.esc(roleLabel)" in source


def test_model_picker_display_name_is_escaped_with_provider_logo():
    source = _source("modelPicker.js")

    assert "'</span> ' + displayName" not in source
    assert "'</span> ' + uiModule.esc(displayName)" in source


def test_html_code_runner_uses_sandboxed_preview_not_same_origin_popup():
    source = _source("codeRunner.js")

    assert "window.open('', '_blank'" not in source
    assert "document.write(code)" not in source
    assert "iframe.setAttribute('sandbox', '')" in source
    assert "iframe.srcdoc = code" in source
    assert "Scripts are disabled" in source


def test_memory_dropdown_uses_shared_dismiss_helper():
    source = _source("memory.js")

    assert "bindMenuDismiss" in source
    assert "document.addEventListener('click', () => { if (dropdown.parentNode) dropdown.remove(); }, { once: false });" not in source
