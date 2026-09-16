import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from ai_companion.adapters.discord import DiscordMessenger, to_incoming
from ai_companion.adapters.memory import InMemoryConversationStore
from ai_companion.application.conversation import ConversationService
from ai_companion.application.ports import DeliveryError, ModelTimeout
from ai_companion.config import DiscordSettings
from ai_companion.domain import ChatResult, ConversationAddress, OutgoingMessage


def message(*, user=123, bot=False, dm=True, text="안녕", id=1):
    channel = MagicMock(spec=discord.DMChannel if dm else discord.TextChannel)
    channel.id = 456
    return SimpleNamespace(
        id=id, author=SimpleNamespace(id=user, bot=bot), channel=channel, content=text
    )


@pytest.mark.parametrize("kwargs", [{"user": 999}, {"bot": True}, {"dm": False}, {"text": " "}])
def test_unwanted_messages_are_filtered(kwargs):
    assert to_incoming(message(**kwargs), frozenset({123})) is None


def test_dm_is_mapped_to_neutral_identity():
    result = to_incoming(message(), frozenset({123}))
    assert result.conversation.address == ConversationAddress("discord", "456")
    assert result.conversation.user.user_id == "123"
    assert result.id == "1" and result.text == "안녕"


async def test_shutdown_cancels_inflight_handler_without_discord_login():
    messenger = DiscordMessenger(DiscordSettings("fake", frozenset({123}), 1))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(incoming):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with messenger:
        messenger._handler = handler
        await messenger.setup_hook()
        await messenger.on_message(message())
        await asyncio.wait_for(started.wait(), 1)
    assert cancelled.is_set()


async def test_send_disables_mentions_and_returns_receipt():
    messenger = DiscordMessenger(DiscordSettings("fake", frozenset({123}), 1))
    channel = MagicMock(spec=discord.DMChannel)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=777))
    messenger.get_channel = MagicMock(return_value=channel)
    async with messenger:
        receipt = await messenger.send(
            ConversationAddress("discord", "456"), OutgoingMessage("@everyone")
        )
        assert receipt.external_message_id == "777"
        assert channel.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}


async def test_sender_rejects_other_platform_and_oversized_unicode():
    messenger = DiscordMessenger(DiscordSettings("fake", frozenset({123}), 1))
    async with messenger:
        with pytest.raises(DeliveryError):
            await messenger.send(ConversationAddress("other", "456"), OutgoingMessage("안녕"))
        with pytest.raises(DeliveryError):
            await messenger.send(
                ConversationAddress("discord", "456"), OutgoingMessage("😀" * 1001)
            )


def typing_messenger(failure_stage):
    messenger = DiscordMessenger(DiscordSettings("fake", frozenset({123}), 1))
    channel = MagicMock(spec=discord.DMChannel)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=777))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=None)
    context.__aexit__ = AsyncMock(return_value=None)
    if failure_stage is not None:
        method = context.__aenter__ if failure_stage == "enter" else context.__aexit__
        method.side_effect = discord.HTTPException(
            SimpleNamespace(status=500, reason="test"), "private-provider-body"
        )
    channel.typing.return_value = context
    messenger.get_channel = MagicMock(return_value=channel)
    return messenger, channel


@pytest.mark.parametrize("failure_stage", [None, "enter", "exit"])
async def test_typing_failure_still_generates_and_delivers_once(failure_stage, caplog):
    messenger, channel = typing_messenger(failure_stage)
    model = SimpleNamespace(generate=AsyncMock(return_value=ChatResult("반가워!")))
    store = InMemoryConversationStore()
    service = ConversationService(model, messenger, store, "짧게 답해")
    incoming = to_incoming(message(), frozenset({123}))
    async with messenger:
        await service.handle(incoming)
        await service.handle(incoming)
    model.generate.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert channel.send.call_args.args == ("반가워!",)
    assert len(store.recent(incoming.conversation)) == 2
    assert "private-provider-body" not in caplog.text


@pytest.mark.parametrize("failure_stage", [None, "enter", "exit"])
@pytest.mark.parametrize("error_kind", ["http", "model", "cancel"])
async def test_typing_preserves_body_errors_and_cancellation(failure_stage, error_kind):
    messenger, _ = typing_messenger(failure_stage)
    errors = {
        "http": discord.HTTPException(SimpleNamespace(status=500, reason="test"), "body"),
        "model": ModelTimeout("timeout"),
        "cancel": asyncio.CancelledError(),
    }
    error = errors[error_kind]
    async with messenger:
        with pytest.raises(type(error)) as caught:
            async with messenger.typing(ConversationAddress("discord", "456")):
                raise error
    assert caught.value is error
