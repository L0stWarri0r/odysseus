"""Search failures must not return HTTP 200 with exception text.

POST /api/search and POST /api/search/query used to answer HTTP 200
``{error: str(e)}``. Provider exceptions can include the request URL, and
Google Programmable Search puts the API key in that URL. comprehensive_web_search
also copied that text into the context string Compare and the agent treat as
a successful result.

Validation stays a fixed sentence at HTTP 400. Unexpected failures are HTTP 502
with ``Search failed``. Success stays HTTP 200 and does not grow an ``error`` key.
"""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.search_routes import setup_search_routes


SECRET = "SUPERSECRET_google_key"
LEAK = (
    "Client error '403 Forbidden' for url "
    "'https://www.googleapis.com/customsearch/v1?key=" + SECRET + "&q=hi'"
)


@pytest.fixture
def client(monkeypatch):
    import routes.search_routes as routes

    monkeypatch.setattr(routes, "comprehensive_web_search", lambda *a, **k: ("ctx", []))
    monkeypatch.setattr(routes, "_call_provider", lambda *a, **k: [{"title": "Hit", "url": "https://example.com"}])
    app = FastAPI()
    app.include_router(setup_search_routes({}))
    return TestClient(app)


def test_web_search_exception_is_502_without_exception_text(client, monkeypatch):
    import routes.search_routes as routes

    def boom(*_a, **_k):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(routes, "comprehensive_web_search", boom)
    response = client.post("/api/search", json={"query": "hello"})
    body = response.json()
    assert response.status_code == 502
    assert body["error"] == "Search failed"
    assert body["context"] == ""
    assert body["sources"] == []
    assert SECRET not in response.text
    assert "googleapis" not in response.text
    assert "403" not in response.text


def test_web_search_missing_query_is_400_and_does_not_search(client, monkeypatch):
    import routes.search_routes as routes

    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("search should not run")

    monkeypatch.setattr(routes, "comprehensive_web_search", boom)
    response = client.post("/api/search", json={"query": "   "})
    body = response.json()
    assert response.status_code == 400
    assert body["error"] == "query is required"
    assert body["context"] == ""
    assert body["sources"] == []
    assert called["n"] == 0


def test_web_search_success_stays_200_without_error_key(client):
    response = client.post("/api/search", data={"query": "hello", "time_filter": "week"})
    body = response.json()
    assert response.status_code == 200
    assert body == {"context": "ctx", "sources": []}
    assert "error" not in body


def test_web_search_passes_provider_failure_context_through(client, monkeypatch):
    import routes.search_routes as routes

    message = "Web search failed — all providers errored or returned empty. Tried: google_pse:error"

    def failed(*_a, **_k):
        return (message, [])

    monkeypatch.setattr(routes, "comprehensive_web_search", failed)
    response = client.post("/api/search", json={"q": "hello"})
    body = response.json()
    assert response.status_code == 200
    assert body["context"] == message
    assert body["sources"] == []
    assert "error" not in body


def test_provider_query_exception_is_502_without_exception_text(client, monkeypatch):
    import routes.search_routes as routes

    def boom(*_a, **_k):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(routes, "_call_provider", boom)
    response = client.post(
        "/api/search/query",
        json={"query": "hello", "provider": "google_pse", "count": 3},
    )
    body = response.json()
    assert response.status_code == 502
    assert body["error"] == "Search failed"
    assert body["results"] == []
    assert body["provider"] == "google_pse"
    assert isinstance(body["time"], (int, float))
    assert SECRET not in response.text
    assert "googleapis" not in response.text


def test_provider_query_missing_query_is_400(client, monkeypatch):
    import routes.search_routes as routes

    def boom(*_a, **_k):
        raise AssertionError("provider should not run")

    monkeypatch.setattr(routes, "_call_provider", boom)
    response = client.post("/api/search/query", json={"provider": "brave"})
    body = response.json()
    assert response.status_code == 400
    assert body["error"] == "query is required"
    assert body["results"] == []
    assert body["provider"] == "brave"
    assert "time" not in body


def test_provider_query_unknown_provider_is_400(client, monkeypatch):
    import routes.search_routes as routes

    def boom(*_a, **_k):
        raise AssertionError("provider should not run")

    monkeypatch.setattr(routes, "_call_provider", boom)
    response = client.post(
        "/api/search/query",
        json={"query": "hello", "provider": "disabled"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Unknown provider"
    response = client.post(
        "/api/search/query",
        json={"query": "hello", "provider": "not-a-provider"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Unknown provider"


def test_provider_query_success_stays_200_without_error_key(client):
    response = client.post(
        "/api/search/query",
        data={"query": "hello", "provider": "duckduckgo", "count": "10"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["provider"] == "duckduckgo"
    assert body["results"][0]["title"] == "Hit"
    assert "error" not in body
    assert "time" in body


def test_comprehensive_search_omits_exception_text_from_context(monkeypatch):
    core = importlib.import_module("services.search.core")
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "google_pse"})
    monkeypatch.setattr(core, "_get_result_count", lambda: 5)

    def boom(*_a, **_k):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(core, "_call_provider", boom)
    context, sources = core.comprehensive_web_search("hi", return_sources=True)
    assert sources == []
    assert SECRET not in context
    assert "googleapis" not in context
    assert "google_pse" in context
    assert "error" in context
    assert context.startswith("Web search failed")
