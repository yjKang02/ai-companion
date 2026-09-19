"""외부 SDK와 무관한 공통 데이터."""

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ConversationAddress:
    platform: str
    channel_id: str


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    platform: str
    user_id: str


@dataclass(frozen=True, slots=True)
class ConversationKey:
    address: ConversationAddress
    user: ExternalIdentity


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    id: str
    conversation: ConversationKey
    text: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    text: str
    finish_reason: str | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    text: str


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    external_message_id: str


@dataclass(frozen=True, slots=True)
class ModelConnection:
    id: str
    provider: str
    base_url: str
    secret_ref: str | None = None
    enabled: bool = True
    revision: int = 1


@dataclass(frozen=True, slots=True)
class MessengerConnection:
    id: str
    platform: str
    secret_ref: str
    enabled: bool = True
    revision: int = 1


@dataclass(frozen=True, slots=True)
class RoomModelConfig:
    """이동 가능한 방 콘텐츠. 로컬 연결이나 비밀 참조를 포함하지 않는다."""

    model_id: str
    temperature: float = 0.7
    max_tokens: int = 256
    timeout: float = 60

    def validate_options(self) -> None:
        """공급자 범위와 별개로 숫자 타입과 유한성을 검증한다. bool은 숫자가 아니다."""
        if type(self.max_tokens) is not int:
            raise ValueError("max_tokens는 정수여야 합니다.")
        for name, value in (("temperature", self.temperature), ("timeout", self.timeout)):
            if type(value) not in (int, float) or (
                isinstance(value, float) and not isfinite(value)
            ):
                raise ValueError(f"{name}은 유한한 숫자여야 합니다.")

    def select(self, connection_id: str) -> "ModelSelection":
        return ModelSelection(
            connection_id, self.model_id, self.temperature, self.max_tokens, self.timeout
        )


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """실행 시에만 조립하는 모델 선택. 방 콘텐츠 저장에 사용하지 않는다."""

    connection_id: str
    model_id: str
    temperature: float = 0.7
    max_tokens: int = 256
    timeout: float = 60

    @property
    def config(self) -> RoomModelConfig:
        return RoomModelConfig(self.model_id, self.temperature, self.max_tokens, self.timeout)

    def validate_options(self) -> None:
        self.config.validate_options()


@dataclass(frozen=True, slots=True)
class RoomRuntimeBinding:
    """개인 설치 내부의 실행 연결. 해제도 generation을 증가시킨다."""

    room_id: str
    connection_id: str | None = None
    generation: int = 0


@dataclass(frozen=True, slots=True)
class RoomContext:
    character: str = field(repr=False)
    user: str = field(default="", repr=False)
    instructions: str = field(default="", repr=False)

    def messages(self) -> tuple[ChatMessage, ...]:
        sections = (
            ("방 지시", self.instructions),
            ("캐릭터", self.character),
            ("사용자 페르소나", self.user),
        )
        return tuple(
            ChatMessage(Role.SYSTEM, f"{label}\n{text}") for label, text in sections if text
        )


@dataclass(frozen=True, slots=True)
class Room:
    id: str
    name: str
    context: RoomContext
    model: RoomModelConfig
    revision: int = 1


@dataclass(frozen=True, slots=True)
class RoomInput:
    room_id: str
    source: str
    request_id: str
    text: str = field(repr=False)


class TurnState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class RoomTurn:
    input: RoomInput
    room_revision: int
    state: TurnState = TurnState.PENDING
    result: ChatResult | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class InputRoute:
    """인증된 외부 입력의 경로. 웹 인증 주체도 호출 경계에서 확인한다."""

    connection_id: str
    platform: str
    channel_id: str
    user_id: str
