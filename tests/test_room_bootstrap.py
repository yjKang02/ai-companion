import httpx
import pytest

from ai_companion import bootstrap
from ai_companion.adapters.room_memory import (
    InMemoryModelConnections,
    InMemoryRoomBindings,
    InMemoryRoomStore,
)
from ai_companion.domain import ModelConnection, RoomContext, RoomInput, RoomModelConfig


class NoSecrets:
    async def get(self, reference):
        pytest.fail("인증 없는 연결에서 비밀값을 요청했습니다.")


async def test_composition_executes_room_and_closes_shared_client_on_failure(monkeypatch):
    def handle(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "응답"}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", lambda **kwargs: client)
    connections = InMemoryModelConnections()
    connections.register(ModelConnection("local", "lmstudio", "http://localhost:1234/v1"))
    with pytest.raises(RuntimeError, match="종료"):
        async with bootstrap.room_backend(
            InMemoryRoomStore(), connections, NoSecrets(), InMemoryRoomBindings()
        ) as service:
            room = await service.create("방", RoomContext("친구"), RoomModelConfig("test"))
            await service.bind(room.id, room.revision, 0, "local")
            result = await service.respond(RoomInput(room.id, "web", "1", "안녕"))
            assert result.result.text == "응답"
            assert not client.is_closed
            raise RuntimeError("종료")
    assert client.is_closed
