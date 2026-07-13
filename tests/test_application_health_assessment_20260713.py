from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_idx = source.index(start)
    end_idx = source.index(end, start_idx)
    return source[start_idx:end_idx]


def test_generated_image_owner_check_is_strict_and_fails_closed():
    block = _between(
        _read("app.py"),
        '@app.get("/api/generated-image/{filename}")',
        "# ========= YOUTUBE INIT =========",
    )

    assert "require_user(request)" in block
    assert "_user = effective_user(request)" in block
    assert "_row is not None and _row.owner != _user" in block
    assert "_row.owner and _row.owner != _user" not in block
    assert "Image ownership check unavailable" in block
    assert "except Exception:\n        pass" not in block


def test_research_endpoint_selection_is_owner_scoped():
    source = _read("routes/research_routes.py")
    start_block = _between(
        source,
        '@router.post("/api/research/start")',
        '@router.get("/api/research/stream/{session_id}")',
    )
    spinoff_block = _between(
        source,
        '@router.post("/api/research/spinoff/{session_id}")',
        '    return router',
    )

    assert "owner=getattr(sess, \"owner\", None)" in source
    assert "ModelEndpoint.id == body.endpoint_id" in start_block
    assert "q = owner_filter(q, ModelEndpoint, user)" in start_block
    assert 'resolve_endpoint("research", owner=user)' in start_block
    assert 'resolve_endpoint("utility", owner=user)' in start_block
    assert 'resolve_endpoint("default", owner=user)' in start_block
    assert 'resolve_endpoint("chat", owner=user)' in start_block
    assert "q = owner_filter(q, ModelEndpoint, user)" in spinoff_block


def test_compare_endpoint_api_key_lookup_is_owner_scoped():
    block = _between(
        _read("routes/compare_routes.py"),
        "def start_comparison(",
        "        # Blind mapping:",
    )

    assert "user = get_current_user(request)" in block
    assert "from src.auth_helpers import owner_filter" in block
    assert "q = db.query(ModelEndpoint).filter(" in block
    assert "q = owner_filter(q, ModelEndpoint, user)" in block
    assert "ep = q.first()" in block
    assert "ModelEndpoint.base_url == base\n                ).first()" not in block


def test_document_tabs_escape_user_controlled_labels():
    block = _between(
        _read("static/js/document.js"),
        "  function renderTabs() {",
        "    // Wire scroll arrows",
    )

    assert "const safeId = uiModule.esc(id);" in block
    assert "const safeTitle = uiModule.esc(title);" in block
    assert "const safeShortTitle = uiModule.esc(shortTitle);" in block
    assert 'title="${safeTitle}"' in block
    assert '<span class="doc-tab-title">${safeShortTitle}</span>' in block
    assert 'title="${title}"' not in block
    assert '<span class="doc-tab-title">${shortTitle}</span>' not in block


def test_group_bubble_role_label_is_escaped():
    block = _between(
        _read("static/js/group.js"),
        "function _createGroupBubble(",
        "async function _sendParallel(",
    )

    assert "${uiModule.esc(roleLabel)}" in block
    assert "${roleLabel}" not in block


def test_compare_search_result_urls_are_scheme_filtered():
    block = _between(
        _read("static/js/compare/stream.js"),
        "function _safeSearchResultHref(",
        "/** Run synthesis for a search pane",
    )

    assert "parsed.protocol === 'http:' || parsed.protocol === 'https:'" in block
    assert "return '#';" in block
    assert "titleLink.href = _safeSearchResultHref(r.url);" in block
    assert "titleLink.href = r.url || '#';" not in block


def test_mermaid_uses_strict_security_level():
    block = _between(
        _read("static/js/markdown.js"),
        "function initMermaid() {",
        "window.odysseusInitMermaid = initMermaid;",
    )

    assert "securityLevel: 'strict'" in block
    assert "securityLevel: 'loose'" not in block
