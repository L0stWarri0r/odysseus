"""Search routes — /api/search/config GET, /api/search POST."""

import logging
from typing import Dict, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import time

from services.search import get_search_config, comprehensive_web_search, PROVIDER_INFO
from services.search.core import _call_provider
from services.search.providers import _get_provider_key, _get_search_instance

logger = logging.getLogger(__name__)


def search_error(message: str, status_code: int = 502, **fields) -> JSONResponse:
    """Client-facing search failure. ``message`` is a fixed sentence.

    Provider exceptions can include the request URL. Google Programmable
    Search puts the API key in that URL, so ``str(e)`` must stay in the log.
    """
    body = dict(fields)
    body["error"] = message
    return JSONResponse(status_code=status_code, content=body)


async def _request_values(request: Request) -> Dict[str, Any]:
    """Accept JSON, form data, or query params for search endpoints.

    The browser UI posts FormData, while the agent's generic app_api tool
    posts JSON. FastAPI Form(...) rejects JSON with a 422 before our handler
    runs, which made the model think SearXNG was broken.
    """
    values: Dict[str, Any] = dict(request.query_params)
    content_type = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict):
                values.update(body)
        else:
            form = await request.form()
            values.update(dict(form))
    except Exception:
        pass
    return values


def setup_search_routes(config) -> APIRouter:
    router = APIRouter(tags=["search"])

    @router.get("/api/search/config")
    async def get_search_settings() -> Dict[str, Any]:
        return get_search_config()

    @router.post("/api/search")
    async def do_web_search(request: Request) -> Dict[str, Any]:
        """Standalone web search — returns context string + source list.

        Used by Compare mode to pre-search once and share results across panes.
        """
        values = await _request_values(request)
        query = str(values.get("query") or values.get("q") or "").strip()
        if not query:
            return search_error("query is required", 400, context="", sources=[])
        time_filter = values.get("time_filter") or values.get("freshness")
        if time_filter is not None:
            time_filter = str(time_filter).strip() or None
        try:
            context, sources = comprehensive_web_search(
                query, return_sources=True, time_filter=time_filter,
            )
            return {"context": context, "sources": sources}
        except Exception:
            logger.exception("Standalone web search failed")
            return search_error("Search failed", 502, context="", sources=[])

    @router.get("/api/search/providers")
    async def list_search_providers():
        """Return available search providers with config status."""
        providers = []
        for pid, (label, needs_key, needs_url) in PROVIDER_INFO.items():
            if pid == "disabled":
                continue
            available = True
            if needs_key and not _get_provider_key(pid):
                available = False
            if needs_url and pid == "searxng" and not _get_search_instance():
                available = False
            providers.append({
                "id": pid,
                "label": label,
                "available": available,
            })
        return providers

    @router.post("/api/search/query")
    async def search_with_provider(request: Request) -> Dict[str, Any]:
        """Search using a specific provider. Used by compare search mode."""
        values = await _request_values(request)
        query = str(values.get("query") or values.get("q") or "").strip()
        provider = str(values.get("provider") or "").strip()
        try:
            count = int(values.get("count") or values.get("limit") or 10)
        except Exception:
            count = 10
        if not query:
            return search_error(
                "query is required", 400, results=[], provider=provider,
            )
        if provider not in PROVIDER_INFO or provider == "disabled":
            return search_error(
                "Unknown provider", 400, results=[], provider=provider,
            )
        t0 = time.time()
        try:
            results = _call_provider(provider, query, min(count, 20))
            elapsed = round(time.time() - t0, 2)
            return {"results": results, "provider": provider, "time": elapsed}
        except Exception:
            elapsed = round(time.time() - t0, 2)
            logger.exception("Search provider %s failed", provider)
            return search_error(
                "Search failed", 502, results=[], provider=provider, time=elapsed,
            )

    return router
