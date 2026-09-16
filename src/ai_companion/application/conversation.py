import asyncio
import logging
from collections import OrderedDict

from ai_companion.application.ports import (
    ChatModel,
    ConversationStore,
    DeliveryError,
    InvalidModelOutput,
    MessageSink,
    ModelError,
)
from ai_companion.domain import (
    ChatMessage,
    ChatRequest,
    ConversationKey,
    IncomingMessage,
    OutgoingMessage,
    Role,
)

logger = logging.getLogger(__name__)


class ConversationService:
    """메신저·모델 구현체를 모르는 단일 턴 처리 서비스."""

    def __init__(
        self,
        model: ChatModel,
        sink: MessageSink,
        store: ConversationStore,
        system_prompt: str,
    ) -> None:
        self._model = model
        self._sink = sink
        self._store = store
        self._system_prompt = system_prompt
        self._lock = asyncio.Lock()
        self._seen: OrderedDict[tuple[ConversationKey, str], None] = OrderedDict()

    async def handle(self, incoming: IncomingMessage) -> None:
        # 첫 버전은 전체 추론을 직렬화한다. 취소는 그대로 상위 수명 관리로 전달한다.
        async with self._lock:
            text = incoming.text.strip()
            if not text:
                return
            event_key = (incoming.conversation, incoming.id)
            if event_key in self._seen:
                return
            self._seen[event_key] = None
            if len(self._seen) > 2048:
                self._seen.popitem(last=False)
            address = incoming.conversation.address
            try:
                if len(text) > 4000:
                    await self._sink.send(
                        address,
                        OutgoingMessage("메시지가 길어서 처리하지 못했어. 조금 나눠서 보내줘."),
                    )
                    return
                request = ChatRequest(
                    messages=(
                        ChatMessage(Role.SYSTEM, self._system_prompt),
                        *self._store.recent(incoming.conversation),
                        ChatMessage(Role.USER, text),
                    )
                )
                try:
                    async with self._sink.typing(address):
                        result = await self._model.generate(request)
                    reply = result.text.strip()
                    if not reply:
                        raise InvalidModelOutput("모델 응답이 비어 있습니다.")
                    # 첫 버전은 단일 짧은 메시지. 전송된 내용만 다음 대화에 포함한다.
                    if len(reply) > 600:
                        reply = reply[:599] + "…"
                except ModelError as error:
                    logger.warning("model_error kind=%s", type(error).__name__)
                    await self._sink.send(
                        address,
                        OutgoingMessage(
                            "지금 모델에 연결하거나 답변을 만들지 못했어. 잠시 뒤 다시 말해줘."
                        ),
                    )
                    return
                await self._sink.send(address, OutgoingMessage(reply))
                self._store.append_turn(incoming.conversation, text, reply)
            except DeliveryError:
                # 외부 송신 성공 여부가 불명확할 수 있으므로 자동 재전송하지 않는다.
                logger.warning("delivery_error")
