"""연결 구조 검증 전용. 재시작 보존을 제공하지 않는다."""

from dataclasses import replace
from uuid import uuid4

from ai_companion.application.room_ports import (
    ConnectionUnavailable,
    InputConflict,
    RevisionConflict,
    RoomNotFound,
    RouteNotFound,
)
from ai_companion.domain import (
    ChatMessage,
    ChatResult,
    InputReceipt,
    InputRoute,
    ModelConnection,
    Role,
    Room,
    RoomRuntimeBinding,
    RoomTurn,
    StoredRoomInput,
    TurnState,
)


class InMemoryModelConnections:
    def __init__(self) -> None:
        self._items: dict[str, ModelConnection] = {}

    def register(self, connection: ModelConnection) -> None:
        if not connection.id or connection.revision != 1 or connection.id in self._items:
            raise RevisionConflict("연결 ID 또는 초기 revision이 올바르지 않습니다.")
        self._items[connection.id] = connection

    def update(self, connection: ModelConnection, expected_revision: int) -> None:
        current = self._items.get(connection.id)
        if (
            current is None
            or current.revision != expected_revision
            or connection.revision != expected_revision + 1
        ):
            raise RevisionConflict("연결이 변경되었습니다.")
        self._items[connection.id] = connection

    async def get(self, connection_id: str) -> ModelConnection:
        try:
            return self._items[connection_id]
        except KeyError:
            raise ConnectionUnavailable("등록된 모델 연결을 찾을 수 없습니다.") from None


class InMemoryRoomBindings:
    def __init__(self) -> None:
        self._bindings: dict[str, RoomRuntimeBinding] = {}

    async def get(self, room_id: str) -> RoomRuntimeBinding:
        return self._bindings.get(room_id, RoomRuntimeBinding(room_id))

    async def set(
        self, room_id: str, connection_id: str | None, expected_generation: int
    ) -> RoomRuntimeBinding:
        current = self._bindings.get(room_id, RoomRuntimeBinding(room_id))
        if current.generation != expected_generation:
            raise RevisionConflict("방의 실행 연결이 변경되었습니다.")
        binding = RoomRuntimeBinding(room_id, connection_id, expected_generation + 1)
        self._bindings[room_id] = binding
        return binding

    async def delete(self, room_id: str) -> None:
        self._bindings.pop(room_id, None)


class InMemoryInputReceipts:
    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str], InputReceipt] = {}

    async def reserve(self, room_id: str, source: str, request_id: str) -> InputReceipt:
        if not room_id.strip() or not source.strip() or not request_id.strip():
            raise ValueError("방과 입력 식별자가 필요합니다.")
        key = (source, request_id)
        previous = self._receipts.get(key)
        if previous is not None:
            if previous.room_id != room_id:
                raise InputConflict("이미 다른 방에 배정된 입력입니다.")
            return previous
        receipt = InputReceipt(str(uuid4()), room_id, source, request_id)
        self._receipts[key] = receipt
        return receipt

    async def mark_accepted(self, receipt: InputReceipt) -> None:
        key = (receipt.source, receipt.request_id)
        previous = self._receipts.get(key)
        if previous is None or replace(previous, accepted=False) != replace(
            receipt, accepted=False
        ):
            raise InputConflict("예약되지 않은 입력입니다.")
        self._receipts[key] = replace(previous, accepted=True)

    async def delete(self, room_id: str) -> None:
        self._receipts = {
            key: value for key, value in self._receipts.items() if value.room_id != room_id
        }


class InMemoryRoomRoutes:
    def __init__(self) -> None:
        self._routes: dict[InputRoute, str] = {}

    def bind(self, route: InputRoute, room_id: str) -> None:
        if route in self._routes:
            raise RevisionConflict("이미 연결된 입력 경로입니다.")
        self._routes[route] = room_id

    def unbind(self, route: InputRoute) -> None:
        self._routes.pop(route, None)

    async def resolve(self, route: InputRoute) -> str:
        try:
            return self._routes[route]
        except KeyError:
            raise RouteNotFound("연결된 방이 없습니다.") from None


class InMemoryRoomStore:
    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}
        self._used_ids: set[str] = set()
        self._history: dict[str, tuple[ChatMessage, ...]] = {}
        self._turns: dict[tuple[str, str], RoomTurn] = {}

    async def create(self, room: Room) -> None:
        if not room.id or room.id in self._used_ids or room.revision != 1:
            raise RevisionConflict("방 ID를 재사용하거나 초기 revision을 바꿀 수 없습니다.")
        self._used_ids.add(room.id)
        self._rooms[room.id] = room
        self._history[room.id] = ()

    async def get(self, room_id: str) -> Room:
        try:
            return self._rooms[room_id]
        except KeyError:
            raise RoomNotFound("방을 찾을 수 없습니다.") from None

    async def update(self, room: Room, expected_revision: int) -> None:
        current = await self.get(room.id)
        if current.revision != expected_revision or room.revision != expected_revision + 1:
            raise RevisionConflict("방 설정이 변경되었습니다.")
        self._rooms[room.id] = room

    async def delete(self, room_id: str, expected_revision: int) -> None:
        current = await self.get(room_id)
        if current.revision != expected_revision:
            raise RevisionConflict("방 설정이 변경되었습니다.")
        del self._rooms[room_id]
        del self._history[room_id]
        self._turns = {key: value for key, value in self._turns.items() if key[0] != room_id}

    async def history(self, room_id: str) -> tuple[ChatMessage, ...]:
        await self.get(room_id)
        return self._history[room_id]

    async def accept(
        self, incoming: StoredRoomInput, expected_revision: int, *, allow_new: bool = True
    ) -> tuple[RoomTurn, bool]:
        if not isinstance(incoming, StoredRoomInput):
            raise TypeError("방 저장소에는 외부 식별자 없는 입력만 전달할 수 있습니다.")
        if not incoming.id.strip() or not incoming.text.strip():
            raise ValueError("내부 입력 ID와 본문이 필요합니다.")
        room = await self.get(incoming.room_id)
        if room.revision != expected_revision:
            raise RevisionConflict("방 설정이 변경되었습니다.")
        key = (room.id, incoming.id)
        if key in self._turns:
            previous = self._turns[key]
            if previous.input != incoming:
                raise InputConflict("같은 입력 ID에 다른 내용이 있습니다.")
            return previous, False
        if not allow_new:
            raise InputConflict("접수된 입력의 방 기록이 없습니다. 복구 확인이 필요합니다.")
        turn = RoomTurn(incoming, expected_revision)
        self._turns[key] = turn
        return turn, True

    async def finish(
        self, turn: RoomTurn, state: TurnState, result: ChatResult | None = None
    ) -> RoomTurn:
        if state == TurnState.PENDING or (state == TurnState.COMPLETED and result is None):
            raise ValueError("완료 상태와 결과를 확인하세요.")
        key = (turn.input.room_id, turn.input.id)
        room = self._rooms.get(turn.input.room_id)
        if room is None:
            return replace(turn, state=TurnState.SUPERSEDED, result=None)
        stored = self._turns.get(key)
        if (
            stored is None
            or stored.input != turn.input
            or stored.room_revision != turn.room_revision
        ):
            raise InputConflict("접수되지 않은 결과입니다.")
        if stored.state != TurnState.PENDING:
            return stored
        if room.revision != turn.room_revision:
            state, result = TurnState.SUPERSEDED, None
        finished = replace(
            stored, state=state, result=result if state == TurnState.COMPLETED else None
        )
        if finished.result is not None:
            self._history[room.id] += (
                ChatMessage(Role.USER, turn.input.text),
                ChatMessage(Role.ASSISTANT, finished.result.text),
            )
        self._turns[key] = finished
        return finished
