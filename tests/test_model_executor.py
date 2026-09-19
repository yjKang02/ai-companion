import json
from dataclasses import replace

import httpx
import pytest

from ai_companion.adapters.model_executor import LMStudioExecutor
from ai_companion.adapters.room_memory import (
    InMemoryModelConnections,
    InMemoryRoomBindings,
    InMemoryRoomStore,
)
from ai_companion.application.room_ports import ConnectionUnavailable
from ai_companion.application.rooms import RoomService
from ai_companion.config import ConfigurationError
from ai_companion.domain import (
    ChatMessage,
    ChatRequest,
    ModelConnection,
    ModelSelection,
    Role,
    RoomContext,
    RoomModelConfig,
)


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


class RecordingStore(InMemoryRoomStore):
    def __init__(self):
        super().__init__()
        self.writes = []

    async def create(self, room):
        self.writes.append("create")
        await super().create(room)

    async def update(self, room, expected_revision):
        self.writes.append("update")
        await super().update(room, expected_revision)


@pytest.mark.parametrize("operation", ["bind", "update"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", 0),
        ("timeout", 601),
        ("timeout", True),
        ("timeout", float("inf")),
        ("timeout", float("-inf")),
        ("timeout", float("nan")),
        ("timeout", "60"),
        ("max_tokens", 0),
        ("max_tokens", 8193),
        ("max_tokens", 1.5),
        ("max_tokens", 1.0),
        ("max_tokens", True),
        ("max_tokens", "32"),
        ("temperature", -0.1),
        ("temperature", 3),
        ("temperature", True),
        ("temperature", float("nan")),
        ("temperature", float("inf")),
        ("temperature", "0.7"),
        ("provider", "unsupported"),
        ("base_url", "http://remote.invalid/v1"),
    ],
)
async def test_binding_or_connected_model_edit_rejects_invalid_settings(operation, field, value):
    def handle(request):
        pytest.fail("설정 저장 중 HTTP 요청이 발생했습니다.")

    store = RecordingStore()
    connections = InMemoryModelConnections()
    connection = ModelConnection("local", "lmstudio", "http://localhost:1234/v1", "key-ref")
    connections.register(connection)
    secrets = Secrets()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bindings = InMemoryRoomBindings()
        service = RoomService(store, connections, LMStudioExecutor(client, secrets), bindings)
        room = await service.create("기존 방", RoomContext("기존 캐릭터"), RoomModelConfig("a"))
        if operation == "update":
            await service.bind(room.id, room.revision, 0, "local")
        model = replace(room.model, model_id="b")
        if field in ("provider", "base_url"):
            connections.update(replace(connection, **{field: value}, revision=2), 1)
        else:
            model = replace(model, **{field: value})
        if operation == "bind":
            # 저장소에서 읽은 미연결 콘텐츠도 연결 시 공급자 검증을 우회할 수 없다.
            room = replace(room, model=model, revision=2)
            await store.update(room, 1)
        previous_writes = list(store.writes)
        previous_binding = await service.binding(room.id)
        with pytest.raises((ValueError, ConfigurationError, ConnectionUnavailable)):
            if operation == "bind":
                await service.bind(room.id, room.revision, 0, "local")
            else:
                await service.update(
                    room.id,
                    room.revision,
                    name="새 이름",
                    context=RoomContext("새 캐릭터"),
                    model=model,
                )
        assert store.writes == previous_writes
        assert await service.binding(room.id) == previous_binding
        assert await store.get(room.id) == room
        assert await store.history(room.id) == ()
    assert secrets.requested == []


@pytest.mark.parametrize(
    "selection",
    [
        ModelSelection("local", "a", temperature=0, max_tokens=1, timeout=1),
        ModelSelection("local", "a", temperature=2, max_tokens=8192, timeout=600),
    ],
)
async def test_valid_boundary_settings_can_be_saved_without_model_or_secret_access(selection):
    def handle(request):
        pytest.fail("설정 검증은 모델에 연결하지 않습니다.")

    store = InMemoryRoomStore()
    connections = InMemoryModelConnections()
    connections.register(
        ModelConnection("local", "lmstudio", "http://localhost:1234/v1", "key-ref")
    )
    secrets = Secrets()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        service = RoomService(
            store, connections, LMStudioExecutor(client, secrets), InMemoryRoomBindings()
        )
        room = await service.create("방", RoomContext("캐릭터"), selection.config)
        await service.bind(room.id, room.revision, 0, selection.connection_id)
        edited = await service.update(
            room.id, room.revision, name="편집", context=room.context, model=selection.config
        )
        assert edited.revision == room.revision + 1
        assert await store.get(room.id) == edited
    assert secrets.requested == []
