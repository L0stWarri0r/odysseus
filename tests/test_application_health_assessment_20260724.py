"""Regression coverage for the 2026-07-24 application health assessment."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_idx = source.index(start)
    end_idx = source.index(end, start_idx)
    return source[start_idx:end_idx]


def test_require_admin_internal_token_requires_trusted_loopback():
    source = _read("core/middleware.py")
    assert "def is_trusted_loopback(request: Request) -> bool:" in source
    block = _between(source, "def require_admin(request: Request):", "class SecurityHeadersMiddleware")
    assert "is_trusted_loopback(request)" in block
    assert "secrets.compare_digest(hdr, INTERNAL_TOOL_TOKEN)" in block


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
    assert "Cache-Control\": \"private," in block or "Cache-Control\": 'private," in block


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
        "    return router",
    )

    assert 'owner=getattr(sess, "owner", None)' in source
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
    assert "owner_filter(q, ModelEndpoint, user)" in block
    assert "Choose a registered model endpoint" in block
    assert "ModelEndpoint.base_url == base\n                ).first()" not in block


def test_gallery_image_tools_scope_endpoint_credentials():
    source = _read("routes/gallery_routes.py")
    assert source.count("q = owner_filter(q, ModelEndpoint, user)") >= 4
    upscale = _between(source, '@router.post("/api/gallery/ai-upscale")', '@router.post("/api/gallery/style-transfer")')
    assert "user = require_privilege(request, \"can_generate_images\")" in upscale
    assert "owner_filter(q, ModelEndpoint, user)" in upscale


def test_hermes_continuity_and_preflight_require_admin():
    source = _read("routes/hermes_routes.py")
    preflight = _between(source, '@router.post("/api/hermes/preflight")', '@router.get("/api/hermes/continuity/inventory")')
    inventory = _between(source, '@router.get("/api/hermes/continuity/inventory")', '@router.get("/api/hermes/maintenance/status")')
    assert "_require_admin(request)" in preflight
    assert "_require_admin(request)" in inventory


def test_service_worker_activate_only_deletes_odysseus_caches():
    block = _between(_read("static/sw.js"), "self.addEventListener('activate'", "self.addEventListener('fetch'")
    assert "k.startsWith('odysseus-')" in block
    assert "keys.filter(k => k !== CACHE_NAME)" not in block


def test_document_tabs_escape_user_controlled_labels():
    block = _between(
        _read("static/js/document.js"),
        "  function renderTabs() {",
        "    // Wire scroll arrows",
    )

    assert "const safeId = uiModule.esc(String(id));" in block
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


def test_group_option_values_are_escaped():
    source = _read("static/js/group.js")
    assert "uiModule.esc(c.id)" in source
    assert "uiModule.esc(m.mid)" in source
    assert "value=\"' + c.id + '\"" not in source
    assert "value=\"' + m.mid + '\"" not in source
    assert 'value="${c.id}"' not in source


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
