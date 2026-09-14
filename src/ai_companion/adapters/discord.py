import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord

from ai_companion.application.ports import DeliveryError, MessageHandler
from ai_companion.config import DiscordSettings
from ai_companion.domain import (
    ConversationAddress,
    ConversationKey,
    DeliveryReceipt,
    ExternalIdentity,
    IncomingMessage,
    OutgoingMessage,
)

logger = logging.getLogger(__name__)


def to_incoming(
    message: discord.Message, allowed_user_ids: frozenset[int]
) -> IncomingMessage | None:
    """허용된 1:1 DM만 공급자 중립 데이터로 바꾼다."""
    if (
        message.author.bot
        or message.author.id not in allowed_user_ids
        or not isinstance(message.channel, discord.DMChannel)
        or not message.content.strip()
    ):
        return None
    return IncomingMessage(
        id=str(message.id),
        conversation=ConversationKey(
            address=ConversationAddress("discord", str(message.channel.id)),
            user=ExternalIdentity("discord", str(message.author.id)),
        ),
        text=message.content,
    )


class DiscordMessenger(discord.Client):
    """discord.py 수명과 bounded queue를 소유하는 수신·송신 어댑터."""

    def __init__(self, settings: DiscordSettings) -> None:
        intents = discord.Intents.none()
        intents.dm_messages = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self._settings = settings
        self._handler: MessageHandler | None = None
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(maxsize=settings.queue_size)
        self._worker: asyncio.Task[None] | None = None

    async def start_receiving(self, handler: MessageHandler) -> None:
        self._handler = handler
        await self.start(self._settings.token)

    async def setup_hook(self) -> None:
        self._worker = asyncio.create_task(self._consume(), name="discord-message-worker")

    async def on_ready(self) -> None:
        logger.info("discord_ready")

    async def on_message(self, message: discord.Message) -> None:
        incoming = to_incoming(message, self._settings.allowed_user_ids)
        if incoming is None:
            return
        try:
            self._queue.put_nowait(incoming)
        except asyncio.QueueFull:
            # 과부하 시 입력을 더 쌓거나 자동 응답 폭주를 만들지 않는다.
            logger.warning("discord_queue_full message_dropped")

    async def _consume(self) -> None:
        while True:
            incoming = await self._queue.get()
            try:
                if self._handler is not None:
                    await self._handler(incoming)
            except Exception as error:
                # 사용자 메시지나 외부 예외 본문을 로그에 남기지 않는다.
                logger.error("message_handler_failed kind=%s", type(error).__name__)
            finally:
                self._queue.task_done()

    async def _dm_channel(self, address: ConversationAddress) -> discord.DMChannel:
        if address.platform != "discord" or not address.channel_id.isdigit():
            raise DeliveryError("Discord 대화 주소가 아닙니다.")
        channel_id = int(address.channel_id)
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
        except (discord.HTTPException, discord.InvalidData):
            raise DeliveryError("Discord 채널 조회 실패") from None
        if not isinstance(channel, discord.DMChannel):
            raise DeliveryError("1:1 DM 채널만 지원합니다.")
        return channel

    async def send(self, address: ConversationAddress, message: OutgoingMessage) -> DeliveryReceipt:
        if not message.text.strip() or len(message.text.encode("utf-16-le")) // 2 > 2000:
            raise DeliveryError("Discord 텍스트 길이 범위를 벗어났습니다.")
        channel = await self._dm_channel(address)
        try:
            result = await channel.send(
                message.text, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            raise DeliveryError("Discord 메시지 전송 실패 또는 결과 불명") from None
        return DeliveryReceipt(str(result.id))

    @asynccontextmanager
    async def typing(self, address: ConversationAddress) -> AsyncIterator[None]:
        channel = await self._dm_channel(address)
        context = channel.typing()
        try:
            await context.__aenter__()
        except discord.HTTPException:
            logger.warning("discord_typing_failed stage=enter")
            entered = False
        else:
            entered = True
        try:
            # 표시 실패와 무관하게 본문을 실행하고, 본문의 예외·취소는 전파한다.
            yield
        finally:
            if entered:
                try:
                    await context.__aexit__(*sys.exc_info())
                except discord.HTTPException:
                    logger.warning("discord_typing_failed stage=exit")

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        await super().close()
