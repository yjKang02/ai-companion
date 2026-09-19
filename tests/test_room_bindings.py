import asyncio
import json
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest

from ai_companion.adapters.room_memory import (
    InMemoryInputReceipts,
    InMemoryModelConnections,
    InMemoryRoomBindings,
    InMemoryRoomStore,
)
from ai_companion.application.room_ports import (
    ConnectionUnavailable,
    RevisionConflict,
    RoomNotFound,
)
from ai_companion.application.rooms import RoomService
from ai_companion.domain import (
    ChatResult,
    ModelConnection,
    ModelSelection,
    RoomContext,
    RoomInput,
    RoomModelConfig,
    TurnState,
)


class Executor:
    def __init__(self):
        self.validated = []
        self.generated = []

    def validate(self, connection, selection):
        self.validated.append((connection, selection))

    async def generate(self, connection, selection, request):
        self.generated.append((connection, selection, request))
        return ChatResult("응답")


@pytest.fixture
def backend():
    rooms = InMemoryRoomStore()
    connections = InMemoryModelConnections()
    connections.register(
        ModelConnection(
            "private-connection", "lmstudio", "http://localhost:1234/v1", "private-secret"
        )
    )
    connections.register(
        ModelConnection("other-connection", "lmstudio", "http://localhost:2345/v1")
    )
    bindings = InMemoryRoomBindings()
    executor = Executor()
    service = RoomService(rooms, connections, executor, bindings, InMemoryInputReceipts())
    return service, rooms, connections, bindings, executor


async def create(service):
    return await service.create(
        "방", RoomContext("캐릭터", "사용자", "지시"), RoomModelConfig("model")
    )


async def test_unbound_room_can_be_created_and_edited_but_cannot_run(backend):
    service, rooms, _, _, executor = backend
    room = await create(service)
    assert (await service.binding(room.id)).generation == 0
    edited = await service.update(
        room.id, 1, name="편집", context=RoomContext("새 역할"), model=room.model
    )
    assert await rooms.get(room.id) == edited
    assert await service.get(room.id) == edited
    assert await service.history(room.id) == ()
    assert executor.validated == []
    request = RoomInput(room.id, "web", "1", "안녕")
    with pytest.raises(ConnectionUnavailable):
        await service.respond(request)
    assert executor.generated == []
    await service.bind(room.id, edited.revision, 0, "private-connection")
    assert (await service.respond(request)).state == TurnState.FAILED
    assert (await service.respond(replace(request, request_id="2"))).state == TurnState.COMPLETED


async def test_room_payload_excludes_connection_and_binding_changes_preserve_content(backend):
    service, rooms, _, _, executor = backend
    first, second = await create(service), await create(service)
    initial = asdict(first)
    await service.bind(first.id, 1, 0, "private-connection")
    await service.bind(second.id, 1, 0, "private-connection")
    await service.bind(first.id, 1, 1, "other-connection")
    assert asdict(await rooms.get(first.id)) == initial
    assert (await service.binding(second.id)).connection_id == "private-connection"
    serialized = json.dumps(initial, ensure_ascii=False)
    for private in (
        "connection_id",
        "private-connection",
        "private-secret",
        "localhost",
        "generation",
    ):
        assert private not in serialized
    await service.respond(RoomInput(first.id, "web", "1", "안녕"))
    assert executor.generated[0][1] == first.model.select("other-connection")
    assert await rooms.history(second.id) == ()


async def test_context_edit_survives_disabled_connection_but_model_change_requires_validation(
    backend,
):
    service, rooms, connections, _, _ = backend
    room = await create(service)
    await service.bind(room.id, 1, 0, "private-connection")
    connection = await connections.get("private-connection")
    connections.update(replace(connection, enabled=False, revision=2), 1)
    edited = await service.update(
        room.id, 1, name="편집", context=RoomContext("역할"), model=room.model
    )
    with pytest.raises(ConnectionUnavailable):
        await service.update(
            room.id, 2, name="실패", context=edited.context, model=RoomModelConfig("new-model")
        )
    assert await rooms.get(room.id) == edited
    await service.bind(room.id, 2, 1, None)
    unbound_edit = await service.update(
        room.id, 2, name="미연결 편집", context=edited.context, model=RoomModelConfig("new-model")
    )
    assert unbound_edit.revision == 3


async def test_stale_binding_or_room_revision_cannot_overwrite_selection(backend):
    service, rooms, _, _, executor = backend
    room = await create(service)
    first_binding = await service.bind(room.id, 1, 0, "private-connection")
    with pytest.raises(RevisionConflict):
        await service.bind(room.id, 1, 0, "other-connection")
    edited = await service.update(room.id, 1, name="편집", context=room.context, model=room.model)
    with pytest.raises(RevisionConflict):
        await service.bind(room.id, 1, 1, "other-connection")
    assert await service.binding(room.id) == first_binding
    assert await rooms.get(room.id) == edited
    assert len(executor.validated) == 1


async def test_concurrent_binding_updates_have_one_winner(backend):
    service, _, _, _, _ = backend
    room = await create(service)
    results = await asyncio.gather(
        service.bind(room.id, 1, 0, "private-connection"),
        service.bind(room.id, 1, 0, "other-connection"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert (await service.binding(room.id)).generation == 1


@pytest.mark.parametrize("target", ["missing", "other-connection"])
async def test_failed_rebinding_preserves_existing_binding_and_content(backend, target):
    service, _, connections, _, _ = backend
    room = await create(service)
    binding = await service.bind(room.id, 1, 0, "private-connection")
    other = await connections.get("other-connection")
    connections.update(replace(other, enabled=False, revision=2), 1)
    with pytest.raises(ConnectionUnavailable):
        await service.bind(room.id, 1, 1, target)
    assert await service.binding(room.id) == binding
    assert await service.get(room.id) == room


async def test_deleted_room_cannot_be_rebound_and_other_bindings_survive(backend):
    service, _, connections, bindings, _ = backend
    first, second = await create(service), await create(service)
    await service.bind(first.id, 1, 0, "private-connection")
    other_binding = await service.bind(second.id, 1, 0, "private-connection")
    with pytest.raises(RevisionConflict):
        await service.delete(first.id, 2)
    assert (await service.binding(first.id)).generation == 1
    await service.delete(first.id, 1)
    with pytest.raises(RoomNotFound):
        await service.bind(first.id, 1, 0, "private-connection")
    with pytest.raises(RoomNotFound):
        await service.binding(first.id)
    assert (await bindings.get(first.id)).connection_id is None
    assert await service.binding(second.id) == other_binding
    assert (await connections.get("private-connection")).enabled


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", float("nan")),
        ("temperature", float("inf")),
        ("timeout", True),
        ("timeout", "60"),
        ("max_tokens", True),
        ("max_tokens", 1.5),
    ],
)
async def test_unbound_creation_still_rejects_invalid_common_types_before_writing(
    backend, field, value
):
    service, rooms, _, _, executor = backend
    rooms.create = AsyncMock(wraps=rooms.create)
    with pytest.raises(ValueError):
        await service.create(
            "방", RoomContext("역할"), replace(RoomModelConfig("model"), **{field: value})
        )
    rooms.create.assert_not_called()
    assert executor.validated == []


async def test_execution_selection_cannot_be_saved_as_room_content(backend):
    service, rooms, _, _, _ = backend
    rooms.create = AsyncMock(wraps=rooms.create)
    with pytest.raises(TypeError):
        await service.create(
            "방", RoomContext("역할"), ModelSelection("private-connection", "model")
        )
    rooms.create.assert_not_called()


async def test_binding_cannot_change_between_result_validation_and_commit(backend):
    service, rooms, _, _, _ = backend
    room = await create(service)
    await service.bind(room.id, 1, 0, "private-connection")
    original_finish = rooms.finish
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_finish(turn, state, result=None):
        entered.set()
        await release.wait()
        return await original_finish(turn, state, result)

    rooms.finish = slow_finish
    responding = asyncio.create_task(service.respond(RoomInput(room.id, "web", "1", "안녕")))
    rebinding = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        rebinding = asyncio.create_task(service.bind(room.id, 1, 1, "other-connection"))
        await asyncio.sleep(0)
        assert not rebinding.done()
    finally:
        release.set()
        result = await responding
        if rebinding is not None:
            await rebinding
    assert result.state == TurnState.COMPLETED
    assert (await service.binding(room.id)).connection_id == "other-connection"
