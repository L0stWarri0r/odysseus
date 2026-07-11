from pathlib import Path


def _read(rel_path: str) -> str:
    return Path(rel_path).read_text(encoding="utf-8")


def test_compare_endpoint_lookup_is_owner_scoped_and_registered_for_non_admins():
    src = _read("routes/compare_routes.py")

    assert "owner_filter(q, ModelEndpoint, user)" in src
    assert "Choose a registered model endpoint" in src
    assert "ModelEndpoint.base_url == base" not in src


def test_research_endpoint_selection_is_owner_scoped():
    src = _read("routes/research_routes.py")

    assert 'resolve_endpoint("research", owner=user)' in src
    assert 'resolve_endpoint("utility", owner=user)' in src
    assert 'resolve_endpoint("default", owner=user)' in src
    assert 'resolve_endpoint("chat", owner=user)' in src
    assert 'owner=getattr(sess, "owner", None)' in src
    assert src.count("owner_filter(q, ModelEndpoint, user)") >= 3


def test_gallery_image_endpoint_selection_is_owner_scoped():
    src = _read("routes/gallery_routes.py")

    assert "from src.auth_helpers import get_current_user, owner_filter, require_privilege" in src
    assert src.count("owner_filter(q, ModelEndpoint, user)") >= 6


def test_chat_header_recovery_uses_effective_owner():
    src = _read("routes/chat_routes.py")

    assert "from src.auth_helpers import effective_user, get_current_user" in src
    assert "resolve_session_auth(sess, session, owner=effective_user(request))" in src


def test_frontend_dynamic_labels_and_links_are_sanitized():
    assert "uiModule.esc(roleLabel)" in _read("static/js/group.js")

    markdown = _read("static/js/markdown.js")
    assert "export function safeLinkUrl" in markdown
    assert "safeLinkUrl" in markdown.split("const markdownModule = {", 1)[1]

    assert "markdownModule.safeLinkUrl(r.url)" in _read("static/js/compare/stream.js")
    assert "_safeHref(s.url || '')" in _read("static/js/research/panel.js")
    assert "markdownModule.safeLinkUrl(src.url || '')" in _read("static/js/documentLibrary.js")
    calendar = _read("static/js/calendar.js")
    assert "import markdownModule from './markdown.js';" in calendar
    assert "markdownModule.safeLinkUrl(url)" in calendar


def test_frontend_document_level_handlers_do_not_accumulate():
    gallery = _read("static/js/gallery.js")
    assert "document.removeEventListener('click', _detailMenuDismiss)" in gallery
    assert "_detailMenuDismiss = null" in gallery

    assert "{ once: true }" in _read("static/js/memory.js")

    document = _read("static/js/document.js")
    assert "let _docPanelAbort = null" in document
    assert "_docPanelAbort = new AbortController()" in document
    assert "_docPanelAbort.abort()" in document
    assert "signal: _docPanelAbort.signal" in document
