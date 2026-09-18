"""방 실행의 저장·연결 포트. 실제 DB 배치나 SDK에 의존하지 않는다."""

from typing import Protocol

from ai_companion.domain import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    InputRoute,
    ModelConnection,
    ModelSelection,
    Room,
    RoomInput,
    RoomTurn,
    TurnState,
)


class RoomNotFound(Exception):
    pass


class RevisionConflict(Exception):
    pass


class InputConflict(Exception):
    pass


class RouteNotFound(Exception):
    pass


class ConnectionUnavailable(Exception):
    pass


class ModelConnections(Protocol):
    async def get(self, connection_id: str) -> ModelConnection: ...


class SecretProvider(Protocol):
    async def get(self, reference: str) -> str: ...


class ModelExecutor(Protocol):
    def validate(self, connection: ModelConnection, selection: ModelSelection) -> None:
        """지원 연결·주소·옵션을 검증한다. 비밀 조회나 네트워크 I/O를 하지 않는다."""
        ...

    async def generate(
        self, connection: ModelConnection, selection: ModelSelection, request: ChatRequest
    ) -> ChatResult: ...


class RoomRoutes(Protocol):
    async def resolve(self, route: InputRoute) -> str: ...


class RoomStore(Protocol):
    async def create(self, room: Room) -> None: ...

    async def get(self, room_id: str) -> Room: ...

    async def update(self, room: Room, expected_revision: int) -> None: ...

    async def delete(self, room_id: str, expected_revision: int) -> None: ...

    async def history(self, room_id: str) -> tuple[ChatMessage, ...]: ...

    async def accept(self, incoming: RoomInput, expected_revision: int) -> tuple[RoomTurn, bool]:
        """입력 접수와 중복 확인을 원자적으로 수행한다. bool은 신규 접수 여부다."""
        ...

    async def finish(
        self, turn: RoomTurn, state: TurnState, result: ChatResult | None = None
    ) -> RoomTurn:
        """방 revision이 다르면 superseded. 삭제된 방은 생성하지 않는다."""
        ...
