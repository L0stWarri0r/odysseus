# src/middleware.py
# Shared middleware, decorators, and request helpers

import os
import secrets

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


# Per-process token that lets the in-app tool layer hit admin-gated
# routes via HTTP loopback (the agent's tool calls don't carry the
# admin user's session cookie). Set once at import; tools read the
# same value from this module. Never persisted or exposed externally.
INTERNAL_TOOL_TOKEN = os.environ.get("ODYSSEUS_INTERNAL_TOKEN") or secrets.token_hex(32)
INTERNAL_TOOL_HEADER = "X-Odysseus-Internal-Token"

# Headers that prove a request was forwarded by a proxy/tunnel. A bare
# client.host loopback check is unsafe behind cloudflared/nginx/Caddy —
# those connect FROM 127.0.0.1, so a remote visitor would inherit local trust.
_PROXY_FWD_HEADERS = (
    "cf-connecting-ip", "cf-ray", "cf-visitor",
    "x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded",
)


def is_trusted_loopback(request: Request) -> bool:
    """True ONLY for a DIRECT loopback connection with no proxy forwarding headers."""
    host = request.client.host if getattr(request, "client", None) else None
    if host not in ("127.0.0.1", "::1"):
        return False
    headers = getattr(request, "headers", None) or {}
    for h in _PROXY_FWD_HEADERS:
        if headers.get(h):
            return False
    return True


# Paths a chat-scoped API bearer token may hit. Default mint scope is "chat";
# without this allowlist the token authenticated as synthetic user "api" and
# could reach research/gallery/email/etc. (DEFAULT_PRIVILEGES for unknown user).
CHAT_SCOPE_EXACT_PATHS = frozenset({
    "/api/v1/chat",
    "/api/companion/ping",
    "/api/companion/info",
    "/api/companion/models",
})
CHAT_SCOPE_PREFIXES = (
    "/api/generated-image/",
)


def api_token_path_allowed(path: str, scopes) -> bool:
    """Return True if a bearer API token with ``scopes`` may access ``path``."""
    scope_set = {str(s).strip() for s in (scopes or []) if str(s).strip()}
    if not scope_set:
        scope_set = {"chat"}
    if "admin" in scope_set or "*" in scope_set:
        return True
    allowed = set()
    if "chat" in scope_set:
        allowed |= CHAT_SCOPE_EXACT_PATHS
    if path in allowed:
        return True
    if "chat" in scope_set and any(path.startswith(p) for p in CHAT_SCOPE_PREFIXES):
        return True
    return False


def require_admin(request: Request):
    """Raise 403 if the current user isn't an admin.
    Allows access when auth is explicitly disabled, or when the request carries
    the in-process internal-tool token used by loopback agent tools.
    """
    # In-process bypass for tool-layer loopback calls. Two paths:
    # (a) header-direct (caller set X-Odysseus-Internal-Token) — ONLY from
    #     trusted loopback, matching AuthMiddleware. Without the loopback
    #     check a leaked/stolen INTERNAL_TOOL_TOKEN would escalate any remote
    #     authenticated non-admin to admin via a single header.
    # (b) the auth middleware already validated the token + loopback and
    #     stamped request.state.current_user = "internal-tool".
    try:
        hdr = request.headers.get(INTERNAL_TOOL_HEADER)
        if hdr and secrets.compare_digest(hdr, INTERNAL_TOOL_TOKEN):
            if is_trusted_loopback(request):
                return
            raise HTTPException(403, "Admin only")
        if getattr(request.state, "current_user", None) == "internal-tool":
            return
    except HTTPException:
        raise
    except Exception:
        pass

    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return
    if not auth_mgr or not auth_mgr.is_configured:
        raise HTTPException(403, "Admin only")
    user = getattr(request.state, "current_user", None)
    if not user or not auth_mgr.is_admin(user):
        raise HTTPException(403, "Admin only")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to all responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate a per-request nonce for inline scripts
        nonce = secrets.token_hex(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)
        path = request.url.path

        # Tool render endpoints are served inside iframes — allow framing by self
        is_tool_render = path.startswith("/api/tools/") and path.endswith("/render")
        # Visual report pages are self-contained HTML — need inline scripts + external images
        is_report = path.startswith("/api/research/report/")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"

        if is_report:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; "
                "img-src 'self' data: blob: https:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
        elif is_tool_render:
            # Tool iframe content: skip all framing headers — the iframe's
            # sandbox="allow-scripts" attribute provides isolation.
            # Don't overwrite the route's own restrictive CSP either.
            pass
        else:
            response.headers["X-Frame-Options"] = "DENY"
            # NOTE: `style-src 'unsafe-inline'` is intentionally retained.
            # `static/index.html` and `static/login.html` ship inline <style>
            # blocks, and several JS modules build runtime `style=""` attrs.
            # Migrating to nonce-only requires templating the HTML files +
            # auditing every JS-set style attribute. Since inline styles
            # don't execute script, the residual risk is visual-only.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "img-src 'self' data: blob:; "
                "media-src 'self' blob:; "
                "connect-src 'self'; "
                "frame-src 'self'; "
                "frame-ancestors 'none'"
            )
        return response
