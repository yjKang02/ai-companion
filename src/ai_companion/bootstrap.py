from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from ai_companion.adapters.discord import DiscordMessenger
from ai_companion.adapters.lmstudio import LMStudioChatModel
from ai_companion.adapters.memory import InMemoryConversationStore
from ai_companion.adapters.model_executor import LMStudioExecutor
from ai_companion.adapters.persistent_rooms import persistent_room_store
from ai_companion.application.conversation import ConversationService
from ai_companion.application.ports import ChatModel, Messenger
from ai_companion.application.room_ports import (
    InputReceipts,
    ModelConnections,
    RoomBindings,
    RoomStore,
    SecretProvider,
)
from ai_companion.application.rooms import RoomService
from ai_companion.config import ConversationSettings, DiscordSettings, ModelSettings
from ai_companion.domain import ChatMessage, ChatRequest, Role


@asynccontextmanager
async def room_backend(
    rooms: RoomStore,
    connections: ModelConnections,
    secrets: SecretProvider,
    bindings: RoomBindings,
    receipts: InputReceipts,
) -> AsyncIterator[RoomService]:
    """새 방 백엔드의 조립 지점. 저장소 수명과 실제 수신은 호출자가 관리한다."""
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        yield RoomService(rooms, connections, LMStudioExecutor(client, secrets), bindings, receipts)


@asynccontextmanager
async def persistent_room_backend(
    runtime_path: Path, library_path: Path, secrets: SecretProvider
) -> AsyncIterator[RoomService]:
    """초기화·v2 이전이 끝난 저장소를 복구하고 조립한다. 종료 전 요청을 모두 끝내야 한다."""
    async with persistent_room_store(runtime_path, library_path) as rooms:
        runtime = rooms.registry.runtime
        async with room_backend(
            rooms, runtime.connections, secrets, runtime.bindings, runtime.receipts
        ) as service:
            yield service


async def run_bot(env: Mapping[str, str]) -> None:
    model_settings = ModelSettings.from_env(env)
    discord_settings = DiscordSettings.from_env(env)
    conversation_settings = ConversationSettings.from_env(env)
    # LM Studio 요청이 시스템 프록시 설정에 의해 우회되지 않게 한다.
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        model: ChatModel = LMStudioChatModel(client, model_settings)
        messenger: Messenger = DiscordMessenger(discord_settings)
        service = ConversationService(
            model=model,
            sink=messenger,
            store=InMemoryConversationStore(conversation_settings.history_messages),
            system_prompt=conversation_settings.system_prompt,
        )
        try:
            await messenger.start_receiving(service.handle)
        finally:
            await messenger.close()


async def check_model(env: Mapping[str, str], prompt: str | None) -> None:
    settings = ModelSettings.from_env(env, require_model=prompt is not None)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        model = LMStudioChatModel(client, settings)
        if prompt is None:
            ids = await model.list_models()
            print("LM Studio 연결 성공. 모델 목록 조회는 실제 생성 성공을 보장하지 않습니다.")
            for model_id in ids:
                print(f"- {model_id}")
            if not ids:
                print("모델 목록이 비어 있습니다. LM Studio에서 모델을 준비하세요.")
        else:
            result = await model.generate(ChatRequest((ChatMessage(Role.USER, prompt),)))
            print(result.text)
