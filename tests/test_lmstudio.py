import asyncio
import json

import httpx
import pytest

from ai_companion.adapters.lmstudio import LMStudioChatModel
from ai_companion.application.ports import (
    InvalidModelOutput,
    ModelAuthenticationError,
    ModelRateLimited,
    ModelTimeout,
    ModelUnavailable,
)
from ai_companion.config import ModelSettings
from ai_companion.domain import ChatMessage, ChatRequest, Role


def settings(**overrides):
    values = dict(
        base_url="http://127.0.0.1:1234/v1",
        model="test-model",
        api_key="",
        timeout=2,
        max_tokens=128,
        temperature=0.7,
    )
    values.update(overrides)
    return ModelSettings(**values)


def request():
    return ChatRequest((ChatMessage(Role.SYSTEM, "짧게"), ChatMessage(Role.USER, "안녕")))


def completion(content="반가워"):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
    }


async def test_wire_format_auth_and_normalized_response():
    def respond(req):
        assert str(req.url) == "http://127.0.0.1:1234/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer test-secret"
        body = json.loads(req.content)
        expected = {
            "model": "test-model",
            "max_tokens": 128,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": "짧게"},
                {"role": "user", "content": "안녕"},
            ],
        }
        assert expected.items() <= body.items()
        body = completion()
        body["choices"][0]["message"]["reasoning_content"] = "노출하면 안 됨"
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await LMStudioChatModel(client, settings(api_key="test-secret")).generate(
            request()
        )
    assert result.text == "반가워"
    assert result.usage.input_tokens == 10


async def test_model_list_without_auth_or_model_selection():
    def respond(req):
        assert req.url.path == "/v1/models"
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await LMStudioChatModel(client, settings(model="")).list_models() == ("model-a",)


@pytest.mark.parametrize(
    "status,error",
    [
        (401, ModelAuthenticationError),
        (403, ModelAuthenticationError),
        (429, ModelRateLimited),
        (404, ModelUnavailable),
        (500, ModelUnavailable),
        (302, ModelUnavailable),
    ],
)
async def test_http_error_mapping_does_not_leak_body(status, error):
    def respond(req):
        return httpx.Response(status, text="private-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(error) as caught:
            await LMStudioChatModel(client, settings()).generate(request())
    assert "private-body" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"choices": []},
        completion(None),
        completion(" "),
        completion("<think>private</think>안녕"),
    ],
)
async def test_invalid_or_private_output_is_rejected(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(InvalidModelOutput):
            await LMStudioChatModel(client, settings()).generate(request())


async def test_non_json_is_rejected():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text="private-html"))
    ) as client:
        with pytest.raises(InvalidModelOutput):
            await LMStudioChatModel(client, settings()).generate(request())


@pytest.mark.parametrize(
    "error,expected",
    [
        (httpx.ReadTimeout("private-url"), ModelTimeout),
        (httpx.ConnectError("private-url"), ModelUnavailable),
    ],
)
async def test_transport_errors(error, expected):
    def respond(req):
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(expected) as caught:
            await LMStudioChatModel(client, settings()).generate(request())
    assert "private-url" not in str(caught.value)


async def test_total_deadline_is_enforced():
    async def respond(req):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ModelTimeout):
            await LMStudioChatModel(client, settings(timeout=0.01)).generate(request())
