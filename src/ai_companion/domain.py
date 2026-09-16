"""외부 SDK와 무관한 공통 데이터."""

from dataclasses import dataclass
from enum import StrEnum


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
