"""방별 설정으로 실행하는 대화 서비스. 외부 전송은 호출자 경계의 책임이다."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4
from weakref import WeakValueDictionary

from ai_companion.application.ports import InvalidModelOutput
from ai_companion.application.room_ports import (
    ConnectionUnavailable,
    ModelConnections,
    ModelExecutor,
    RevisionConflict,
    RoomBindings,
    RoomRoutes,
    RoomStore,
)
from ai_companion.domain import (
    ChatMessage,
    ChatRequest,
    InputRoute,
    ModelConnection,
    ModelSelection,
    Role,
    Room,
    RoomContext,
    RoomInput,
    RoomModelConfig,
    RoomRuntimeBinding,
    RoomTurn,
    TurnState,
)


class RoomService:
    def __init__(
        self,
        rooms: RoomStore,
        connections: ModelConnections,
        executor: ModelExecutor,
        bindings: RoomBindings,
    ) -> None:
        self._rooms = rooms
        self._connections = connections
        self._executor = executor
        self._bindings = bindings
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._changes: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _change_lock(self, room_id: str) -> asyncio.Lock:
        return self._changes.setdefault(room_id, asyncio.Lock())

    @staticmethod
    def _check_content(name: str, context: RoomContext, model: RoomModelConfig) -> None:
        if not isinstance(model, RoomModelConfig):
            raise TypeError("방에는 연결 ID 없는 모델 설정만 저장할 수 있습니다.")
        if not name.strip() or not context.character.strip() or not model.model_id.strip():
            raise ValueError("방 이름·캐릭터·모델 ID가 필요합니다.")
        model.validate_options()

    async def _connection(self, selection: ModelSelection) -> ModelConnection:
        connection = await self._connections.get(selection.connection_id)
        if not connection.enabled:
            raise ConnectionUnavailable("모델 연결이 비활성 상태입니다.")
        return connection

    async def create(self, name: str, context: RoomContext, model: RoomModelConfig) -> Room:
        """미연결 방을 생성한다. 실행 가능한 연결은 bind()에서 별도 검증한다."""
        self._check_content(name, context, model)
        room = Room(str(uuid4()), name.strip(), context, model)
        await self._rooms.create(room)
        return room

    async def get(self, room_id: str) -> Room:
        return await self._rooms.get(room_id)

    async def history(self, room_id: str) -> tuple[ChatMessage, ...]:
        return await self._rooms.history(room_id)

    async def update(
        self,
        room_id: str,
        expected_revision: int,
        *,
        name: str,
        context: RoomContext,
        model: RoomModelConfig,
    ) -> Room:
        self._check_content(name, context, model)
        async with self._change_lock(room_id):
            current = await self._rooms.get(room_id)
            if current.revision != expected_revision:
                raise RevisionConflict("방 설정이 변경되었습니다.")
            binding = await self._bindings.get(room_id)
            if model != current.model and binding.connection_id is not None:
                selection = model.select(binding.connection_id)
                self._executor.validate(await self._connection(selection), selection)
            room = Room(room_id, name.strip(), context, model, expected_revision + 1)
            await self._rooms.update(room, expected_revision)
            return room

    async def binding(self, room_id: str) -> RoomRuntimeBinding:
        async with self._change_lock(room_id):
            await self._rooms.get(room_id)
            return await self._bindings.get(room_id)

    async def bind(
        self,
        room_id: str,
        expected_revision: int,
        expected_generation: int,
        connection_id: str | None,
    ) -> RoomRuntimeBinding:
        """방 콘텐츠를 바꾸지 않고 실행 연결을 지정·해제한다."""
        async with self._change_lock(room_id):
            room = await self._rooms.get(room_id)
            current = await self._bindings.get(room_id)
            if room.revision != expected_revision or current.generation != expected_generation:
                raise RevisionConflict("방 또는 실행 연결이 변경되었습니다.")
            if connection_id is not None:
                selection = room.model.select(connection_id)
                self._executor.validate(await self._connection(selection), selection)
            return await self._bindings.set(room_id, connection_id, expected_generation)

    async def delete(self, room_id: str, expected_revision: int) -> None:
        async with self._change_lock(room_id):
            await self._rooms.delete(room_id, expected_revision)
            await self._bindings.delete(room_id)

    async def respond(self, incoming: RoomInput) -> RoomTurn:
        if (
            not incoming.text.strip()
            or not incoming.source.strip()
            or not incoming.request_id.strip()
        ):
            raise ValueError("입력 내용과 입력 식별자가 필요합니다.")
        # 단일 서비스 인스턴스에서 방별 순서를 보장한다. 다른 방은 별도 잠금을 사용한다.
        lock = self._locks.setdefault(incoming.room_id, asyncio.Lock())
        async with lock:
            async with self._change_lock(incoming.room_id):
                room = await self._rooms.get(incoming.room_id)
                binding = await self._bindings.get(room.id)
                turn, accepted = await self._rooms.accept(incoming, room.revision)
            if not accepted:
                return turn
            try:
                if binding.connection_id is None:
                    raise ConnectionUnavailable("방에 실행할 모델 연결이 없습니다.")
                selection = room.model.select(binding.connection_id)
                connection = await self._connection(selection)
                history = await self._rooms.history(room.id)
                request = ChatRequest(
                    (*room.context.messages(), *history, ChatMessage(Role.USER, incoming.text))
                )
                result = await self._executor.generate(connection, selection, request)
                if not result.text.strip():
                    raise InvalidModelOutput("모델 응답이 비어 있습니다.")
                async with self._change_lock(room.id):
                    current = await self._connections.get(connection.id)
                    current_binding = await self._bindings.get(room.id)
                    if current != connection or not current.enabled or current_binding != binding:
                        return await self._rooms.finish(turn, TurnState.SUPERSEDED)
                    return await self._rooms.finish(
                        turn, TurnState.COMPLETED, replace(result, text=result.text.strip())
                    )
            except asyncio.CancelledError:
                await self._rooms.finish(turn, TurnState.CANCELLED)
                raise
            except Exception:
                await self._rooms.finish(turn, TurnState.FAILED)
                raise


class RoutedRoomService:
    """수신 어댑터가 인증한 경로만 받는다. 미연결 입력으로 방을 만들지 않는다."""

    def __init__(self, routes: RoomRoutes, service: RoomService) -> None:
        self._routes = routes
        self._service = service

    async def respond(self, route: InputRoute, request_id: str, text: str) -> RoomTurn:
        room_id = await self._routes.resolve(route)
        # 입력 키는 연결과 플랫폼을 포함하며 본문·토큰을 포함하지 않는다.
        source = json.dumps(
            [route.platform, route.connection_id, route.channel_id, route.user_id],
            ensure_ascii=True,
        )
        return await self._service.respond(RoomInput(room_id, source, request_id, text))
