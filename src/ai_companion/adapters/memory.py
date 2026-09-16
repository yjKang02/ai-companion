from collections import OrderedDict

from ai_companion.domain import ChatMessage, ConversationKey, Role


class InMemoryConversationStore:
    """프로세스 수명 동안만 보존하는 연결 검증용 저장소."""

    def __init__(self, history_messages: int = 20, max_conversations: int = 128) -> None:
        if history_messages < 2 or history_messages % 2 or max_conversations < 1:
            raise ValueError("대화 보존 개수는 2 이상의 짝수, 대화 수는 1 이상이어야 합니다.")
        self._limit = history_messages
        self._capacity = max_conversations
        self._messages: OrderedDict[ConversationKey, tuple[ChatMessage, ...]] = OrderedDict()

    def recent(self, key: ConversationKey) -> tuple[ChatMessage, ...]:
        if key not in self._messages:
            return ()
        self._messages.move_to_end(key)
        return self._messages[key]

    def append_turn(self, key: ConversationKey, user: str, assistant: str) -> None:
        messages = (
            *self.recent(key),
            ChatMessage(Role.USER, user),
            ChatMessage(Role.ASSISTANT, assistant),
        )
        self._messages[key] = messages[-self._limit :]
        self._messages.move_to_end(key)
        if len(self._messages) > self._capacity:
            self._messages.popitem(last=False)
