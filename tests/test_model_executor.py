import json
from dataclasses import replace

import httpx
import pytest

from ai_companion.adapters.model_executor import LMStudioExecutor
from ai_companion.application.room_ports import ConnectionUnavailable
from ai_companion.config import ConfigurationError
from ai_companion.domain import ChatMessage, ChatRequest, ModelConnection, ModelSelection, Role


class Secrets:
    def __init__(self):
        self.requested = []

    async def get(self, reference):
        self.requested.append(reference)
        return "test-only-token"


async def test_executor_uses_room_model_options_and_resolves_secret_at_call_time():
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "응답"}}]}
        )

    secrets = Secrets()
    connection = ModelConnection("local", "lmstudio", "http://localhost:1234/v1", "key-ref")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        executor = LMStudioExecutor(client, secrets)
        for model in ("a", "b"):
            result = await executor.generate(
                connection,
                ModelSelection("local", model, temperature=0.2, max_tokens=32),
                ChatRequest((ChatMessage(Role.USER, "안녕"),)),
            )
            assert result.text == "응답"
    assert [json.loads(request.content)["model"] for request in sent] == ["a", "b"]
    assert all(json.loads(request.content)["max_tokens"] == 32 for request in sent)
    assert all(request.headers["Authorization"] == "Bearer test-only-token" for request in sent)
    assert secrets.requested == ["key-ref", "key-ref"]
    assert "test-only-token" not in repr(connection)


@pytest.mark.parametrize("change", ["provider", "disabled", "mismatch", "url", "option"])
async def test_invalid_connections_and_settings_never_send_or_resolve_secret(change):
    def handle(request):
        pytest.fail("검증 실패한 요청이 전송되었습니다.")

    connection = ModelConnection("local", "lmstudio", "http://localhost:1234/v1", "key-ref")
    selection = ModelSelection("local", "a")
    if change == "provider":
        connection = replace(connection, provider="unsupported")
    elif change == "disabled":
        connection = replace(connection, enabled=False)
    elif change == "mismatch":
        selection = replace(selection, connection_id="other")
    elif change == "url":
        connection = replace(connection, base_url="http://remote.invalid/v1")
    else:
        selection = replace(selection, temperature=float("nan"))
    secrets = Secrets()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises((ConnectionUnavailable, ConfigurationError)):
            await LMStudioExecutor(client, secrets).generate(connection, selection, ChatRequest(()))
    assert secrets.requested == []
