"""방 실행의 저장·연결 포트. 실제 DB 배치나 SDK에 의존하지 않는다."""

from typing import Protocol

from ai_companion.domain import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    InputReceipt,
    InputRoute,
    ModelConnection,
    ModelSelection,
    Room,
    RoomRuntimeBinding,
    RoomTurn,
    StoredRoomInput,
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


class StorageUnavailable(Exception):
    """저장소 접근·손상·스키마 오류. 원문 DB 오류나 개인 경로를 노출하지 않는다."""


class ModelConnections(Protocol):
    async def get(self, connection_id: str) -> ModelConnection: ...


class RoomBindings(Protocol):
    async def get(self, room_id: str) -> RoomRuntimeBinding:
        """미등록은 generation=0인 미연결 값. 방 존재 여부는 호출자가 확인한다."""
        ...

    async def set(
        self, room_id: str, connection_id: str | None, expected_generation: int
    ) -> RoomRuntimeBinding:
        """예상 generation 검사와 증가를 원자적으로 수행한다. None은 연결 해제다."""
        ...

    async def delete(self, room_id: str) -> None:
        """방 삭제 후 내부 바인딩을 제거한다. 없는 바인딩은 무시한다."""
        ...


class SecretProvider(Protocol):
    async def get(self, reference: str) -> str: ...


class InputReceipts(Protocol):
    async def reserve(self, room_id: str, source: str, request_id: str) -> InputReceipt:
        """(source, request_id)에 불투명 ID를 원자적으로 할당. 다른 방 재할당은 충돌."""
        ...

    async def mark_accepted(self, receipt: InputReceipt) -> None:
        """방 입력 저장 후 접수를 표시한다. 같은 receipt의 반복 확정은 허용한다."""
        ...

    async def delete(self, room_id: str) -> None:
        """해당 방의 대응표를 제거한다. 영구 삭제 복구와 재수신 차단은 별도 계약이다."""
        ...


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

    async def list_rooms(self) -> tuple[Room, ...]: ...

    async def get(self, room_id: str) -> Room: ...

    async def update(self, room: Room, expected_revision: int) -> None: ...

    async def delete(self, room_id: str, expected_revision: int) -> None: ...

    async def history(self, room_id: str) -> tuple[ChatMessage, ...]: ...

    async def accept(
        self, incoming: StoredRoomInput, expected_revision: int, *, allow_new: bool = True
    ) -> tuple[RoomTurn, bool]:
        """내부 ID로 원자적 접수·본문 비교. allow_new=False면 누락된 입력을 복원하지 않는다."""
        ...

    async def finish(
        self, turn: RoomTurn, state: TurnState, result: ChatResult | None = None
    ) -> RoomTurn:
        """방 revision이 다르면 superseded. 삭제된 방은 생성하지 않는다."""
        ...
