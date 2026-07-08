from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_research_endpoint_resolution_is_owner_scoped():
    source = _read("routes/research_routes.py")

    assert "resolve_endpoint_by_id(body.endpoint_id, body.model, owner=endpoint_owner or None)" in source
    assert 'resolve_endpoint("research", owner=endpoint_owner or None)' in source
    assert 'resolve_endpoint("utility", owner=endpoint_owner or None)' in source
    assert 'resolve_endpoint("default", owner=endpoint_owner or None)' in source
    assert 'resolve_endpoint("chat", owner=endpoint_owner or None)' in source
    assert "q = owner_filter(q, ModelEndpoint, endpoint_owner)" in source


def test_gallery_image_endpoint_selection_is_owner_scoped():
    source = _read("routes/gallery_routes.py")

    assert "def _image_endpoint_query(db, request: Request, user: str):" in source
    assert "q = owner_filter(q, ModelEndpoint, endpoint_owner)" in source
    assert source.count("_image_endpoint_query(db, request, user).first()") >= 2
    assert source.count("_image_endpoint_query(db, request, user).all()") >= 4
    assert "db.query(ModelEndpoint).all()" not in source
    assert 'db.query(ModelEndpoint).filter(ModelEndpoint.model_type == "image"' not in source


def test_generated_image_ownership_check_fails_closed():
    source = _read("app.py")

    assert "Generated image ownership check failed" in source
    assert 'raise HTTPException(status_code=503, detail="Image ownership check unavailable")' in source
    assert "\n    except Exception:\n        pass\n    ext = filename.rsplit" not in source


def test_document_library_create_uses_session_contract():
    source = _read("static/js/documentLibrary.js")

    create_block = source[source.index("createBtn.addEventListener('click'"):]
    create_block = create_block[:create_block.index("    // Archived toggle")]
    assert "new FormData()" in create_block
    assert "fd.append('name', 'Untitled Document')" in create_block
    assert "fd.append('skip_validation', 'true')" in create_block
    assert "const sessionId = sData.id;" in create_block
    assert "window.sessionModule.selectSession(sessionId)" in create_block
    assert "sData.session_id" not in create_block
    assert "window.sessionsModule" not in create_block


def test_session_library_restore_uses_unarchive_route():
    source = _read("static/js/sessions.js")

    assert "/api/session/${sid}/unarchive" in source
    assert "/api/session/${s.id}/unarchive" in source
    assert "/api/session/${sid}/restore" not in source
    assert "/api/session/${s.id}/restore" not in source


def test_dynamic_frontend_labels_and_errors_are_escaped():
    group = _read("static/js/group.js")
    picker = _read("static/js/modelPicker.js")
    chat_renderer = _read("static/js/chatRenderer.js")
    doc_library = _read("static/js/documentLibrary.js")
    document = _read("static/js/document.js")

    assert "${uiModule.esc(roleLabel)}" in group
    assert "${uiModule.esc(_providerDisplayName(provider))}" in picker
    assert "uiModule.esc(displayName)" in picker
    assert "uiModule.esc(err.message || err)" in chat_renderer
    assert "Failed to load: ${_esc(e.message || e)}" in doc_library
    assert "Failed to load preview: ${_esc((e && e.message) || e)}" in document
