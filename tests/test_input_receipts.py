import asyncio
from dataclasses import asdict, replace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_companion.adapters.room_memory import (
    InMemoryInputReceipts,
    InMemoryModelConnections,
    InMemoryRoomBindings,
    InMemoryRoomRoutes,
    InMemoryRoomStore,
)
from ai_companion.application.room_ports import InputConflict, RoomNotFound
from ai_companion.application.rooms import RoomService, RoutedRoomService
from ai_companion.domain import (
    ChatResult,
    InputRoute,
    ModelConnection,
    RoomContext,
    RoomInput,
    RoomModelConfig,
    StoredRoomInput,
    TurnState,
)


async def backend():
    rooms = InMemoryRoomStore()
    receipts = InMemoryInputReceipts()
    connections = InMemoryModelConnections()
    connections.register(ModelConnection("local", "lmstudio", "http://localhost:1234/v1"))
    executor = Mock()
    executor.generate = AsyncMock(return_value=ChatResult("응답"))
    service = RoomService(rooms, connections, executor, InMemoryRoomBindings(), receipts)
    room = await service.create("방", RoomContext("캐릭터"), RoomModelConfig("model"))
    await service.bind(room.id, room.revision, 0, "local")
    return service, rooms, receipts, executor, room


async def test_external_identifiers_stay_in_receipts_not_room_turns():
    service, rooms, receipts, executor, room = await backend()
    routes = InMemoryRoomRoutes()
    route = InputRoute("private-connection", "telegram", "private-chat", "private-user")
    routes.bind(route, room.id)
    routed = RoutedRoomService(routes, service)
    turn = await routed.respond(route, "private-message", "비공개 본문")
    assert set(asdict(turn.input)) == {"id", "room_id", "text"}
    assert turn.input.text == "비공개 본문"
    for external_id in (*asdict(route).values(), "private-message"):
        assert external_id not in str(asdict(turn))
    assert "비공개 본문" not in repr(turn)
    duplicate = await routed.respond(route, "private-message", "비공개 본문")
    assert duplicate == turn
    assert len(await rooms.history(room.id)) == 2
    executor.generate.assert_awaited_once()


async def test_receipts_are_body_free_namespace_scoped_and_atomically_reserved():
    receipts = InMemoryInputReceipts()
    first, duplicate = await asyncio.gather(
        receipts.reserve("room", "source", "request"),
        receipts.reserve("room", "source", "request"),
    )
    assert first == duplicate and not first.accepted
    assert set(asdict(first)) == {"input_id", "room_id", "source", "request_id", "accepted"}
    other = await receipts.reserve("room", "other-source", "request")
    assert other.input_id != first.input_id
    await receipts.mark_accepted(first)
    await receipts.mark_accepted(first)
    assert (await receipts.reserve("room", "source", "request")).accepted
    with pytest.raises(InputConflict):
        await receipts.mark_accepted(replace(first, input_id="forged"))
    with pytest.raises(InputConflict):
        await receipts.mark_accepted(replace(first, room_id="forged"))


async def test_same_external_event_cannot_be_assigned_to_two_rooms_concurrently():
    receipts = InMemoryInputReceipts()
    results = await asyncio.gather(
        receipts.reserve("first", "source", "event"),
        receipts.reserve("second", "source", "event"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, InputConflict) for result in results) == 1


async def test_rerouting_does_not_reexecute_an_accepted_event_in_another_room():
    service, rooms, _, executor, room = await backend()
    second = await service.create("다른 방", room.context, room.model)
    await service.bind(second.id, second.revision, 0, "local")
    routes = InMemoryRoomRoutes()
    route = InputRoute("bot", "telegram", "chat", "user")
    routes.bind(route, room.id)
    routed = RoutedRoomService(routes, service)
    await routed.respond(route, "event", "안녕")
    routes.unbind(route)
    routes.bind(route, second.id)
    with pytest.raises(InputConflict):
        await routed.respond(route, "event", "안녕")
    assert await rooms.history(second.id) == ()
    executor.generate.assert_awaited_once()
    await routed.respond(route, "new-event", "새 입력")
    assert len(await rooms.history(second.id)) == 2


async def test_changed_body_conflicts_without_changing_receipt_or_history():
    service, rooms, receipts, executor, room = await backend()
    incoming = RoomInput(room.id, "source", "event", "처음")
    await service.respond(incoming)
    receipt = await receipts.reserve(room.id, "source", "event")
    with pytest.raises(InputConflict):
        await service.respond(replace(incoming, text="변경"))
    assert await receipts.reserve(room.id, "source", "event") == receipt
    assert (await rooms.history(room.id))[0].content == "처음"
    executor.generate.assert_awaited_once()


async def test_room_store_rejects_transport_commands():
    _, rooms, _, _, room = await backend()
    with pytest.raises(TypeError):
        await rooms.accept(RoomInput(room.id, "source", "event", "안녕"), room.revision)


async def test_receipt_reservation_failure_stops_before_room_acceptance(monkeypatch):
    service, rooms, receipts, executor, room = await backend()
    accepting = AsyncMock(wraps=rooms.accept)
    monkeypatch.setattr(rooms, "accept", accepting)
    monkeypatch.setattr(receipts, "reserve", AsyncMock(side_effect=OSError("내부 저장 실패")))
    with pytest.raises(OSError):
        await service.respond(RoomInput(room.id, "source", "event", "안녕"))
    accepting.assert_not_awaited()
    executor.generate.assert_not_awaited()


async def test_failure_before_room_write_retries_with_the_reserved_input_id(monkeypatch):
    service, rooms, receipts, executor, room = await backend()
    accepting = rooms.accept
    monkeypatch.setattr(rooms, "accept", AsyncMock(side_effect=OSError("방 저장 실패")))
    incoming = RoomInput(room.id, "source", "event", "안녕")
    with pytest.raises(OSError):
        await service.respond(incoming)
    receipt = await receipts.reserve(room.id, "source", "event")
    assert not receipt.accepted
    executor.generate.assert_not_awaited()
    monkeypatch.setattr(rooms, "accept", accepting)
    turn = await service.respond(incoming)
    assert turn.input.id == receipt.input_id
    assert turn.state == TurnState.COMPLETED
    executor.generate.assert_awaited_once()


@pytest.mark.parametrize("failure_point", ["room-write", "receipt-before", "receipt-after"])
async def test_uncertain_acceptance_never_automatically_executes_again(monkeypatch, failure_point):
    service, rooms, receipts, executor, room = await backend()
    accepting, marking = rooms.accept, receipts.mark_accepted

    async def uncertain_write(*args, **kwargs):
        await accepting(*args, **kwargs)
        raise OSError("방 저장 결과 불명")

    async def uncertain_mark(receipt):
        if failure_point == "receipt-after":
            await marking(receipt)
        raise OSError("접수 확정 실패")

    if failure_point == "room-write":
        monkeypatch.setattr(rooms, "accept", uncertain_write)
    else:
        monkeypatch.setattr(receipts, "mark_accepted", uncertain_mark)
    incoming = RoomInput(room.id, "source", "event", "안녕")
    with pytest.raises(OSError):
        await service.respond(incoming)
    executor.generate.assert_not_awaited()
    monkeypatch.setattr(rooms, "accept", accepting)
    monkeypatch.setattr(receipts, "mark_accepted", marking)
    turn = await service.respond(incoming)
    assert turn.state == TurnState.PENDING
    assert await rooms.history(room.id) == ()
    executor.generate.assert_not_awaited()


async def test_accepted_receipt_without_room_input_fails_closed():
    service, rooms, receipts, executor, room = await backend()
    receipt = await receipts.reserve(room.id, "source", "event")
    await receipts.mark_accepted(receipt)
    with pytest.raises(InputConflict, match="복구"):
        await service.respond(RoomInput(room.id, "source", "event", "새 본문으로 복원 금지"))
    executor.generate.assert_not_awaited()
    assert await rooms.history(room.id) == ()


async def test_delete_removes_only_owned_receipts_and_late_mark_cannot_restore_them():
    service, rooms, receipts, executor, room = await backend()
    other = await receipts.reserve("other-room", "other-source", "event")
    incoming = RoomInput(room.id, "source", "event", "안녕")
    turn = await service.respond(incoming)
    owned = await receipts.reserve(room.id, "source", "event")
    await service.delete(room.id, room.revision)
    with pytest.raises(RoomNotFound):
        await service.respond(incoming)
    with pytest.raises(InputConflict):
        await receipts.mark_accepted(owned)
    assert await receipts.reserve("other-room", "other-source", "event") == other
    late = await rooms.finish(turn, TurnState.COMPLETED, ChatResult("늦은 응답"))
    assert late.state == TurnState.SUPERSEDED
    executor.generate.assert_awaited_once()


@pytest.mark.parametrize("field", ["room_id", "source", "request_id", "text"])
async def test_invalid_request_never_reserves_or_runs(monkeypatch, field):
    service, _, receipts, executor, room = await backend()
    reserving = AsyncMock(wraps=receipts.reserve)
    monkeypatch.setattr(receipts, "reserve", reserving)
    incoming = replace(RoomInput(room.id, "source", "event", "안녕"), **{field: " "})
    with pytest.raises((ValueError, RoomNotFound)):
        await service.respond(incoming)
    reserving.assert_not_awaited()
    executor.generate.assert_not_awaited()


async def test_stored_input_rejects_empty_id():
    _, rooms, _, _, room = await backend()
    with pytest.raises(ValueError):
        await rooms.accept(StoredRoomInput(" ", room.id, "안녕"), room.revision)
