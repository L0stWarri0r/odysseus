"""Compose-upload tokens must be isolated by owner.

Staged attachments used to live in a shared `_compose/` directory keyed only
by `{uuid}_{filename}`. UUID guessing is hard, but a leaked token could be
attached or deleted by any other authenticated user. Files now live under an
owner prefix of COMPOSE_UPLOADS_DIR; attach/delete/cleanup resolve only inside
the caller's prefix.

These tests do not re-land HTTP-status changes from PR #31 (compose-upload
still returns HTTP 200 `{success:false}` on write failure).
"""

from email.mime.multipart import MIMEMultipart
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _isolate_compose_dir(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    compose_dir = tmp_path / "_compose"
    compose_dir.mkdir()
    monkeypatch.setattr(email_helpers, "COMPOSE_UPLOADS_DIR", compose_dir)
    return compose_dir, email_helpers


def test_owner_segments_differ_for_distinct_owners():
    from routes.email_helpers import _compose_owner_segment

    alice = _compose_owner_segment("alice")
    bob = _compose_owner_segment("bob")
    unscoped = _compose_owner_segment("")
    colliding_a = _compose_owner_segment("alice/bob")
    colliding_b = _compose_owner_segment("alice_bob")

    assert alice != bob
    assert alice != unscoped
    assert colliding_a != colliding_b
    assert "/" not in alice and ".." not in alice.split(".")[0]
    assert unscoped == "_"
    assert _compose_owner_segment("..").startswith("user.")
    assert not _compose_owner_segment("..").startswith("..")


def test_compose_upload_path_stays_inside_owner_prefix(tmp_path, monkeypatch):
    compose_dir, helpers = _isolate_compose_dir(tmp_path, monkeypatch)

    path = helpers._compose_upload_path("aabbcc_secret.pdf", "alice", create=True)
    assert path is not None
    assert path.parent == helpers._compose_owner_dir("alice")
    assert compose_dir in path.parents
    assert path.parent != compose_dir

    escaped = helpers._compose_upload_path("../etc/passwd", "alice")
    assert escaped is not None
    assert escaped.name == "passwd"
    assert escaped.parent == helpers._compose_owner_dir("alice")
    assert escaped != Path("/etc/passwd")

    assert helpers._compose_upload_path("..", "alice") is None
    assert helpers._compose_upload_path("", "alice") is None
    assert helpers._compose_upload_path(".", "alice") is None

    unscoped = helpers._compose_upload_path("aa_file.txt", "", create=True)
    assert unscoped is not None
    assert unscoped.parent == helpers._compose_owner_dir("")
    assert unscoped.parent.name == "_"
    assert unscoped.parent != compose_dir


def test_attach_and_cleanup_ignore_another_owners_token(tmp_path, monkeypatch):
    compose_dir, helpers = _isolate_compose_dir(tmp_path, monkeypatch)

    token = "aabbccdd_secret.pdf"
    alice_path = helpers._compose_upload_path(token, "alice", create=True)
    alice_path.write_bytes(b"alice-private")

    outer = MIMEMultipart("mixed")
    helpers._attach_compose_uploads(outer, [token], owner="bob")
    filenames = [p.get_filename() for p in outer.walk() if p.get_filename()]
    assert "secret.pdf" not in filenames

    helpers._cleanup_compose_uploads([token], owner="bob")
    assert alice_path.exists()
    assert alice_path.read_bytes() == b"alice-private"

    outer_alice = MIMEMultipart("mixed")
    helpers._attach_compose_uploads(outer_alice, [token], owner="alice")
    filenames = [p.get_filename() for p in outer_alice.walk() if p.get_filename()]
    assert filenames == ["secret.pdf"]

    helpers._cleanup_compose_uploads([token], owner="alice")
    assert not alice_path.exists()
    assert not (compose_dir / token).exists()


def test_shared_root_token_is_not_used_for_authenticated_owner(tmp_path, monkeypatch):
    """Legacy files in `_compose/` root must not be attachable once owner prefixes exist."""
    compose_dir, helpers = _isolate_compose_dir(tmp_path, monkeypatch)

    token = "deadbeef_legacy.pdf"
    leaked = compose_dir / token
    leaked.write_bytes(b"legacy-shared")

    outer = MIMEMultipart("mixed")
    helpers._attach_compose_uploads(outer, [token], owner="alice")
    filenames = [p.get_filename() for p in outer.walk() if p.get_filename()]
    assert filenames == []

    helpers._cleanup_compose_uploads([token], owner="alice")
    assert leaked.exists()


@pytest.mark.asyncio
async def test_compose_upload_and_delete_are_owner_prefixed(tmp_path, monkeypatch):
    compose_dir, helpers = _isolate_compose_dir(tmp_path, monkeypatch)
    import routes.email_routes as email_routes

    monkeypatch.setattr(email_routes, "_start_poller", lambda: None)
    router = email_routes.setup_email_routes()
    compose_upload = _route_endpoint(router, "/api/email/compose-upload", "POST")
    delete_upload = _route_endpoint(router, "/api/email/compose-upload/{token}", "DELETE")

    upload = UploadFile(filename="invoice.pdf", file=BytesIO(b"%PDF-1.4 alice"))
    result = await compose_upload(file=upload, owner="alice")
    assert result["success"] is True
    token = result["token"]
    assert result["filename"] == "invoice.pdf"

    alice_path = helpers._compose_upload_path(token, "alice")
    assert alice_path is not None and alice_path.exists()
    assert alice_path.read_bytes() == b"%PDF-1.4 alice"
    assert not (compose_dir / token).exists()

    bob_path = helpers._compose_upload_path(token, "bob")
    assert bob_path is None or not bob_path.exists()

    deleted = await delete_upload(token, owner="bob")
    assert deleted["success"] is True
    assert alice_path.exists()

    deleted = await delete_upload(token, owner="alice")
    assert deleted["success"] is True
    assert not alice_path.exists()


def test_prepare_signed_reply_stages_via_owner_prefixed_helper():
    """Pin the document signed-reply path to the shared helper so it cannot
    silently go back to writing into the shared `_compose` root."""
    src = Path("routes/document_routes.py").read_text(encoding="utf-8")
    assert "_compose_upload_path" in src
    assert '_COMPOSE_DIR = _Path(_BASE) / "_compose"' not in src
