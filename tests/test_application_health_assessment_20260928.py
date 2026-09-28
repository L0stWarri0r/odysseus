"""Trending-model fetch must not answer HTTP 200 with upstream exception text."""

import asyncio

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.cookbook_routes as cookbook_routes
from routes.cookbook_routes import setup_cookbook_routes

_LEAK = "ConnectError secret-hf-token https://user:token@10.1.2.3/proxy"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_cookbook_routes())
    return TestClient(app)


def _patch_hf(monkeypatch, response):
    captured = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return captured


class _Resp:
    def __init__(self, status_code, payload=None, text="SECRET-HF-BODY"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _assert_generic_failure(response):
    assert response.status_code == 502
    assert response.json() == {
        "ok": False,
        "error": "Failed to load trending models",
        "models": [],
    }
    assert "secret-hf-token" not in response.text
    assert "SECRET-HF-BODY" not in response.text
    assert "HF API HTTP" not in response.text


def test_hf_latest_http_error_is_502_without_upstream_text(monkeypatch):
    _patch_hf(monkeypatch, _Resp(503, payload={"message": _LEAK}))
    response = _client(monkeypatch).get("/api/cookbook/hf-latest")
    _assert_generic_failure(response)
    assert "503" not in response.text


def test_hf_latest_exception_is_502_without_exception_text(monkeypatch):
    _patch_hf(monkeypatch, OSError(_LEAK))
    response = _client(monkeypatch).get("/api/cookbook/hf-latest")
    _assert_generic_failure(response)


def test_hf_latest_non_json_body_is_502_without_body(monkeypatch):
    _patch_hf(monkeypatch, _Resp(200, payload=ValueError(_LEAK)))
    response = _client(monkeypatch).get("/api/cookbook/hf-latest")
    _assert_generic_failure(response)


def test_hf_latest_unexpected_payload_is_502(monkeypatch):
    _patch_hf(monkeypatch, _Resp(200, payload={"message": _LEAK}))
    response = _client(monkeypatch).get("/api/cookbook/hf-latest")
    _assert_generic_failure(response)


def test_hf_latest_success_stays_200_and_does_not_trust_env(monkeypatch):
    captured = _patch_hf(monkeypatch, _Resp(200, payload=[
        "not-a-dict",
        {"modelId": 12, "pipeline_tag": "text-generation"},
        {
            "modelId": "org/Demo-7B",
            "downloads": 12,
            "likes": 3,
            "createdAt": "2026-01-02T00:00:00.000Z",
            "tags": ["text-generation", {"bad": True}],
            "pipeline_tag": "text-generation",
        },
        {
            "id": "org/Other-Task",
            "tags": [],
            "pipeline_tag": "fill-mask",
        },
    ]))
    response = _client(monkeypatch).get("/api/cookbook/hf-latest")

    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert [m["repo_id"] for m in body["models"]] == ["org/Demo-7B"]
    assert body["models"][0]["needed_vram_gb"] == 18.2
    assert captured["url"] == "https://huggingface.co/api/models"
    assert captured["params"]["filter"] == "text-generation"
    assert captured["params"]["limit"] == 150
    assert captured["kwargs"]["trust_env"] is False
    assert captured["kwargs"]["follow_redirects"] is False


def test_hf_latest_caps_outbound_pool(monkeypatch):
    captured = _patch_hf(monkeypatch, _Resp(200, payload=[]))
    response = _client(monkeypatch).get("/api/cookbook/hf-latest", params={"limit": 100000})

    assert response.status_code == 200
    assert response.json() == {"models": []}
    assert captured["params"]["limit"] == 200


def test_hf_latest_forwards_search_and_rejects_control_chars(monkeypatch):
    captured = _patch_hf(monkeypatch, _Resp(200, payload=[]))
    client = _client(monkeypatch)
    ok = client.get("/api/cookbook/hf-latest", params={"search": "Qwen 7B"})
    assert ok.status_code == 200
    assert captured["params"]["search"] == "Qwen 7B"

    captured.clear()
    bad = client.get("/api/cookbook/hf-latest", params={"search": "qwen\ninjected"})
    assert bad.status_code == 400
    assert bad.json()["detail"] == "Invalid search"
    assert "url" not in captured


def test_hf_latest_invalid_pipeline_is_400_and_does_not_fetch(monkeypatch):
    captured = _patch_hf(monkeypatch, _Resp(200, payload=[{"modelId": "org/Demo-7B"}]))
    response = _client(monkeypatch).get(
        "/api/cookbook/hf-latest",
        params={"pipeline": "text-generation&evil=1"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid pipeline"
    assert "url" not in captured


def _tool_client(monkeypatch, response):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_search_hf_models_reports_upstream_failure(monkeypatch):
    from src.tool_implementations import do_search_hf_models

    _tool_client(monkeypatch, _Resp(
        502,
        payload={"ok": False, "error": _LEAK, "models": []},
    ))
    result = asyncio.run(do_search_hf_models("{}"))

    assert result == {"error": "Failed to load trending models", "exit_code": 1}
    assert "secret-hf-token" not in str(result)


def test_search_hf_models_hides_transport_exception(monkeypatch):
    from src.tool_implementations import do_search_hf_models

    _tool_client(monkeypatch, OSError(_LEAK))
    result = asyncio.run(do_search_hf_models('{"query": "qwen"}'))

    assert result == {"error": "Failed to load trending models", "exit_code": 1}
    assert "secret-hf-token" not in str(result)


def test_search_hf_models_empty_success_stays_empty(monkeypatch):
    from src.tool_implementations import do_search_hf_models

    _tool_client(monkeypatch, _Resp(200, payload={"models": []}))
    result = asyncio.run(do_search_hf_models('{"query": "missing"}'))

    assert result["exit_code"] == 0
    assert "No models found" in result["output"]


def test_search_hf_models_lists_results(monkeypatch):
    from src.tool_implementations import do_search_hf_models

    _tool_client(monkeypatch, _Resp(200, payload={"models": [{
        "repo_id": "org/Demo-7B",
        "downloads": 4,
        "needed_vram_gb": 18.2,
    }]}))
    result = asyncio.run(do_search_hf_models("{}"))

    assert result["exit_code"] == 0
    assert "org/Demo-7B" in result["output"]
    assert result["models"][0]["repo_id"] == "org/Demo-7B"
