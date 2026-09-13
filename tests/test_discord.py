import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from ai_companion.adapters.discord import DiscordMessenger, to_incoming
from ai_companion.application.ports import DeliveryError
from ai_companion.config import DiscordSettings
from ai_companion.domain import ConversationAddress, OutgoingMessage


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


async def test_bounded_queue_and_shutdown_without_discord_login():
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
        await messenger.on_message(message(id=2))
        await messenger.on_message(message(id=3))
        assert messenger._queue.qsize() == 1
        assert messenger.intents.dm_messages
        assert not messenger.intents.message_content
    assert cancelled.is_set()
    assert messenger._worker is None


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
