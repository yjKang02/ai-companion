import asyncio
from dataclasses import replace

import pytest

from ai_companion.adapters.room_memory import (
    InMemoryInputReceipts,
    InMemoryModelConnections,
    InMemoryRoomBindings,
    InMemoryRoomRoutes,
    InMemoryRoomStore,
)
from ai_companion.application.ports import ModelTimeout
from ai_companion.application.room_ports import (
    ConnectionUnavailable,
    InputConflict,
    RevisionConflict,
    RoomNotFound,
    RouteNotFound,
)
from ai_companion.application.rooms import RoomService, RoutedRoomService
from ai_companion.domain import (
    ChatResult,
    InputRoute,
    ModelConnection,
    RoomContext,
    RoomInput,
    RoomModelConfig,
    TurnState,
)


class Executor:
    def __init__(self):
        self.calls = []
        self.error = None
        self.entered = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def validate(self, connection, selection):
        pass

    async def generate(self, connection, selection, request):
        self.calls.append((connection, selection, request))
        if len(self.calls) == 2:
            self.two_entered.set()
        self.entered.set()
        await self.release.wait()
        if self.error:
            raise self.error
        return ChatResult(f"{selection.model_id} 응답")


def setup():
    store = InMemoryRoomStore()
    connections = InMemoryModelConnections()
    connections.register(ModelConnection("local", "lmstudio", "http://localhost:1234/v1"))
    connections.register(ModelConnection("second", "lmstudio", "http://localhost:2345/v1"))
    executor = Executor()
    return (
        RoomService(store, connections, executor, InMemoryRoomBindings(), InMemoryInputReceipts()),
        store,
        connections,
        executor,
    )


async def create(service, name="방", connection="local", model="model-a"):
    room = await service.create(name, RoomContext("친절한 친구"), RoomModelConfig(model))
    await service.bind(room.id, room.revision, 0, connection)
    return room


def incoming(room, request_id="1", source="web", text="안녕"):
    return RoomInput(room.id, source, request_id, text)


async def test_rooms_share_connection_but_keep_settings_and_history_independent():
    service, store, _, executor = setup()
    first = await create(service)
    second = await create(service, model="model-b")
    assert first.id != second.id
    await service.respond(incoming(first))
    await service.respond(incoming(second, "second-1"))
    await service.respond(incoming(first, "2", text="다시"))
    assert [call[1].model_id for call in executor.calls] == ["model-a", "model-b", "model-a"]
    assert [len(call[2].messages) for call in executor.calls] == [2, 2, 4]
    assert len(await store.history(first.id)) == 4
    assert len(await store.history(second.id)) == 2


async def test_service_leaves_provider_ranges_to_executor():
    service, store, _, _ = setup()
    selection = RoomModelConfig("model-a", temperature=3, max_tokens=10000, timeout=900)
    room = await service.create("방", RoomContext("캐릭터"), selection)
    await service.bind(room.id, room.revision, 0, "local")
    assert (await store.get(room.id)).model == selection


async def test_edit_switches_connection_and_context_without_replacing_room_or_history():
    service, store, _, executor = setup()
    room = await create(service)
    await service.respond(incoming(room))
    edited = await service.update(
        room.id,
        1,
        name="새 이름",
        context=RoomContext("새 역할"),
        model=RoomModelConfig("model-b"),
    )
    await service.bind(room.id, edited.revision, 1, "second")
    await service.respond(incoming(edited, "2"))
    assert edited.id == room.id and edited.revision == 2
    assert executor.calls[-1][0].id == "second"
    assert executor.calls[-1][2].messages[0].content == "캐릭터\n새 역할"
    assert len(await store.history(room.id)) == 4
    with pytest.raises(RevisionConflict):
        await service.update(room.id, 1, name=room.name, context=room.context, model=room.model)


async def test_duplicate_input_returns_original_result_and_changed_body_conflicts():
    service, store, _, executor = setup()
    room = await create(service)
    results = await asyncio.gather(service.respond(incoming(room)), service.respond(incoming(room)))
    assert results[0] == results[1]
    assert len(executor.calls) == 1
    assert len(await store.history(room.id)) == 2
    with pytest.raises(InputConflict):
        await service.respond(incoming(room, text="다른 내용"))
    await service.respond(incoming(room, source="another"))
    assert len(executor.calls) == 2


@pytest.mark.parametrize(
    "action", ["edit", "delete", "connection", "disable", "bind", "unbind", "aba"]
)
async def test_late_results_are_not_saved_after_execution_settings_change(action):
    service, store, connections, executor = setup()
    room = await create(service)
    other = await create(service)
    executor.release.clear()
    task = asyncio.create_task(service.respond(incoming(room)))
    await asyncio.wait_for(executor.entered.wait(), 1)
    if action == "edit":
        await service.update(
            room.id, 1, name="변경", context=RoomContext("다른 역할"), model=room.model
        )
    elif action == "delete":
        await service.delete(room.id, 1)
    elif action in ("bind", "unbind", "aba"):
        await service.bind(room.id, 1, 1, None if action == "unbind" else "second")
        if action == "aba":
            await service.bind(room.id, 1, 2, "local")
    else:
        connection = await connections.get("local")
        connections.update(replace(connection, revision=2, enabled=action != "disable"), 1)
    executor.release.set()
    assert (await task).state == TurnState.SUPERSEDED
    assert await store.history(other.id) == ()
    if action == "delete":
        with pytest.raises(RoomNotFound):
            await store.get(room.id)
        with pytest.raises(RevisionConflict):
            await store.create(room)
    else:
        assert await store.history(room.id) == ()


async def test_failure_is_recorded_without_automatic_retry():
    service, store, _, executor = setup()
    room = await create(service)
    executor.error = ModelTimeout("timeout")
    with pytest.raises(ModelTimeout):
        await service.respond(incoming(room))
    assert (await service.respond(incoming(room))).state == TurnState.FAILED
    assert len(executor.calls) == 1
    assert await store.history(room.id) == ()


async def test_cancellation_is_recorded_and_not_retried():
    service, store, _, executor = setup()
    room = await create(service)
    executor.release.clear()
    task = asyncio.create_task(service.respond(incoming(room)))
    await asyncio.wait_for(executor.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await service.respond(incoming(room))).state == TurnState.CANCELLED
    assert await store.history(room.id) == ()


async def test_other_room_can_run_while_one_room_waits():
    service, _, _, executor = setup()
    first = await create(service)
    second = await create(service)
    executor.release.clear()
    tasks = [
        asyncio.create_task(service.respond(incoming(room, str(index))))
        for index, room in enumerate((first, second))
    ]
    try:
        await asyncio.wait_for(executor.two_entered.wait(), 1)
    finally:
        executor.release.set()
        await asyncio.gather(*tasks)


async def test_missing_or_disabled_connection_is_rejected():
    service, _, connections, executor = setup()
    with pytest.raises(ConnectionUnavailable):
        await create(service, connection="missing")
    connection = await connections.get("local")
    connections.update(replace(connection, enabled=False, revision=2), 1)
    with pytest.raises(ConnectionUnavailable):
        await create(service)
    assert executor.calls == []


async def test_routes_require_exact_authorized_identity_and_connection():
    service, _, _, executor = setup()
    room = await create(service)
    routes = InMemoryRoomRoutes()
    route = InputRoute("bot-a", "discord", "channel", "owner")
    routes.bind(route, room.id)
    routed = RoutedRoomService(routes, service)
    for bad in (replace(route, user_id="stranger"), replace(route, connection_id="bot-b")):
        with pytest.raises(RouteNotFound):
            await routed.respond(bad, "1", "안녕")
    await routed.respond(route, "1", "안녕")
    assert len(executor.calls) == 1
    routes.unbind(route)
    with pytest.raises(RouteNotFound):
        await routed.respond(route, "2", "안녕")


async def test_deleted_route_cannot_recreate_room():
    service, _, _, executor = setup()
    room = await create(service)
    routes = InMemoryRoomRoutes()
    route = InputRoute("bot-a", "discord", "channel", "owner")
    routes.bind(route, room.id)
    await service.delete(room.id, room.revision)
    with pytest.raises(RoomNotFound):
        await RoutedRoomService(routes, service).respond(route, "1", "안녕")
    assert executor.calls == []
