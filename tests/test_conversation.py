import asyncio
from contextlib import asynccontextmanager

import pytest

from ai_companion.adapters.memory import InMemoryConversationStore
from ai_companion.application.conversation import ConversationService
from ai_companion.application.ports import DeliveryError, ModelTimeout
from ai_companion.domain import (
    ChatResult,
    ConversationAddress,
    ConversationKey,
    DeliveryReceipt,
    ExternalIdentity,
    IncomingMessage,
    Role,
)


def incoming(id="1", user="u1", text="안녕", platform="fake"):
    return IncomingMessage(
        id,
        ConversationKey(ConversationAddress(platform, "room"), ExternalIdentity(platform, user)),
        text,
    )


class FakeModel:
    def __init__(self, text="반가워!", error=None):
        self.text = text
        self.error = error
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return ChatResult(self.text)


class FakeMessenger:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.is_typing = False

    async def send(self, address, message):
        if self.fail:
            raise DeliveryError("실패")
        self.sent.append((address, message))
        return DeliveryReceipt(str(len(self.sent)))

    @asynccontextmanager
    async def typing(self, address):
        self.is_typing = True
        try:
            yield
        finally:
            self.is_typing = False


def setup(model=None, sink=None, history=20):
    model = model or FakeModel()
    sink = sink or FakeMessenger()
    store = InMemoryConversationStore(history)
    return ConversationService(model, sink, store, "짧게 답해"), model, sink, store


async def test_two_turns_include_persona_and_delivered_history_once():
    service, model, sink, store = setup()
    await service.handle(incoming())
    await service.handle(incoming(id="2", text="뭐해?"))
    assert [m.role for m in model.requests[1].messages] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
    ]
    assert [m.content for m in model.requests[1].messages] == [
        "짧게 답해",
        "안녕",
        "반가워!",
        "뭐해?",
    ]
    assert len(sink.sent) == 2
    assert len(store.recent(incoming().conversation)) == 4
    assert not sink.is_typing


async def test_duplicate_events_are_processed_once_even_when_concurrent():
    service, model, sink, _ = setup()
    await asyncio.gather(service.handle(incoming()), service.handle(incoming()))
    assert len(model.requests) == len(sink.sent) == 1


async def test_user_and_platform_histories_are_separate():
    service, model, _, _ = setup()
    await service.handle(incoming())
    await service.handle(incoming(user="u2"))
    await service.handle(incoming(platform="second"))
    assert [len(r.messages) for r in model.requests] == [2, 2, 2]


@pytest.mark.parametrize("model", [FakeModel(error=ModelTimeout("timeout")), FakeModel(text=" ")])
async def test_model_failure_sends_one_notice_without_adding_history(model):
    service, model, sink, store = setup(model=model)
    await service.handle(incoming())
    assert len(sink.sent) == 1
    assert sink.sent[0][1].text.strip()
    assert store.recent(incoming().conversation) == ()
    assert not sink.is_typing


async def test_failed_delivery_is_not_recorded_or_retried():
    service, model, _, store = setup(sink=FakeMessenger(fail=True))
    await service.handle(incoming())
    await service.handle(incoming())
    assert len(model.requests) == 1
    assert store.recent(incoming().conversation) == ()


async def test_cancellation_propagates_and_closes_typing():
    started = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            started.set()
            await asyncio.Event().wait()

    service, _, sink, store = setup(model=WaitingModel())
    task = asyncio.create_task(service.handle(incoming()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sink.sent and not sink.is_typing
    assert store.recent(incoming().conversation) == ()


async def test_bounded_history_and_sent_text_match():
    service, _, sink, store = setup(model=FakeModel(text="가" * 900), history=2)
    await service.handle(incoming())
    await service.handle(incoming(id="2", text="둘째"))
    history = store.recent(incoming().conversation)
    assert len(history) == 2 and history[0].content == "둘째"
    assert history[1].content == sink.sent[-1][1].text


async def test_empty_input_does_not_call_model_or_send():
    service, model, sink, _ = setup()
    await service.handle(incoming(text=" "))
    assert not model.requests
    assert not sink.sent


def test_store_evicts_least_recent_conversation():
    store = InMemoryConversationStore(max_conversations=1)
    store.append_turn(incoming().conversation, "안녕", "응")
    store.append_turn(incoming(user="u2").conversation, "둘째", "응")
    assert not store.recent(incoming().conversation)
