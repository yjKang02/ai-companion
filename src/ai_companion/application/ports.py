from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ai_companion.domain import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ConversationAddress,
    ConversationKey,
    DeliveryReceipt,
    IncomingMessage,
    OutgoingMessage,
)

MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class ModelError(Exception):
    """외부 응답 본문이나 비밀 정보를 포함하지 않는 모델 오류."""


class ModelUnavailable(ModelError):
    pass


class ModelTimeout(ModelError):
    pass


class ModelAuthenticationError(ModelError):
    pass


class ModelRateLimited(ModelError):
    pass


class InvalidModelOutput(ModelError):
    pass


class DeliveryError(Exception):
    """전송 실패 또는 결과 불명. 자동 재전송하지 않는다."""


class ChatModel(Protocol):
    async def generate(self, request: ChatRequest) -> ChatResult: ...


class MessageSink(Protocol):
    async def send(
        self, address: ConversationAddress, message: OutgoingMessage
    ) -> DeliveryReceipt: ...

    def typing(self, address: ConversationAddress) -> AbstractAsyncContextManager[None]:
        """지원하지 않는 메신저는 아무 동작 없는 컨텍스트를 반환한다."""
        ...


class Messenger(MessageSink, Protocol):
    async def start_receiving(self, handler: MessageHandler) -> None: ...

    async def close(self) -> None: ...


class ConversationStore(Protocol):
    def recent(self, key: ConversationKey) -> tuple[ChatMessage, ...]: ...

    def append_turn(self, key: ConversationKey, user: str, assistant: str) -> None: ...
