import pytest
import httpx

from src import ai_interaction


class _ImageResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"data": [{"url": "http://169.254.169.254/latest/meta-data"}]}


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _ImageResponse()


@pytest.mark.asyncio
async def test_generate_image_rejects_unsafe_returned_url(monkeypatch):
    monkeypatch.setattr(
        ai_interaction,
        "_resolve_model",
        lambda spec: (
            "https://images.example/v1/chat/completions",
            "dall-e-3",
            {},
        ),
    )
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    def fail_if_downloaded(*args, **kwargs):
        raise AssertionError("unsafe returned image URL was downloaded")

    monkeypatch.setattr(httpx, "get", fail_if_downloaded)

    result = await ai_interaction.do_generate_image(
        "draw a safe test image\ndall-e-3\n1024x1024"
    )

    assert "unsafe image URL" in result["error"]
