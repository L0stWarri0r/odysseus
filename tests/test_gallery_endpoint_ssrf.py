"""Regression: the gallery image-edit proxies must validate a client-supplied
``_endpoint`` through ``check_outbound_url`` before fetching it server-side.

``POST /api/image/harmonize`` and ``POST /api/image/inpaint`` accept an
``_endpoint`` field in the request body and then issue outbound httpx POSTs to
it. With no validation this is a server-side request forgery primitive: a caller
can point ``_endpoint`` at ``http://169.254.169.254/`` (cloud instance metadata)
or at internal/loopback services the server can reach but the caller cannot.

The analogous user-supplied endpoint in ``routes/embedding_routes.py`` already
goes through ``check_outbound_url``; these two routes were missing the same
guard. This test pins the guard in place and confirms the validator rejects the
metadata range.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "routes" / "gallery_routes.py"


def _function_source(src_text: str, func_name: str) -> str:
    tree = ast.parse(src_text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(src_text, node)
    raise AssertionError(f"{func_name} not found in {SRC}")


def test_endpoint_validated_before_fetch():
    src = SRC.read_text()
    for func in ("harmonize_image", "inpaint_proxy"):
        body = _function_source(src, func)
        assert "check_outbound_url" in body, (
            f"{func} must validate the client-supplied _endpoint via "
            "check_outbound_url before issuing an outbound request"
        )


def test_url_safety_blocks_metadata_endpoint():
    # The guard is only as strong as the checker: confirm the link-local cloud
    # metadata address is rejected even with private IPs otherwise allowed.
    from src.url_safety import check_outbound_url
    ok, _ = check_outbound_url("http://169.254.169.254/latest/meta-data")
    assert ok is False


def test_returned_image_url_validated_before_secondary_fetch():
    src = SRC.read_text()
    for func in ("harmonize_image", "inpaint_proxy"):
        body = _function_source(src, func)
        assert "_validate_returned_image_url(item.get(\"url\"))" in body
        assert body.index("_validate_returned_image_url(item.get(\"url\"))") < body.index(
            ".get(item[\"url\"]"
        )


def test_returned_image_url_blocks_metadata_endpoint():
    from fastapi import HTTPException
    from routes.gallery_routes import _validate_returned_image_url

    try:
        _validate_returned_image_url("http://169.254.169.254/latest/meta-data")
    except HTTPException as exc:
        assert exc.status_code == 502
        assert "unsafe image URL" in str(exc.detail)
    else:
        raise AssertionError("metadata URL should be rejected before download")
