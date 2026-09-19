"""공유 HTTP 자원 위에 방별 모델 설정을 적용한다."""

from dataclasses import replace

import httpx

from ai_companion.adapters.lmstudio import LMStudioChatModel
from ai_companion.application.room_ports import ConnectionUnavailable, SecretProvider
from ai_companion.config import ConfigurationError, ModelSettings
from ai_companion.domain import ChatRequest, ChatResult, ModelConnection, ModelSelection


class LMStudioExecutor:
    def __init__(self, client: httpx.AsyncClient, secrets: SecretProvider) -> None:
        self._client = client
        self._secrets = secrets

    def _settings(self, connection: ModelConnection, selection: ModelSelection) -> ModelSettings:
        if (
            not connection.enabled
            or connection.provider != "lmstudio"
            or connection.id != selection.connection_id
        ):
            raise ConnectionUnavailable("지원하지 않거나 사용할 수 없는 모델 연결입니다.")
        try:
            selection.validate_options()
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        return ModelSettings.from_env(
            {
                "LMSTUDIO_BASE_URL": connection.base_url,
                "LMSTUDIO_MODEL": selection.model_id,
                "MODEL_TIMEOUT_SECONDS": str(selection.timeout),
                "MODEL_MAX_TOKENS": str(selection.max_tokens),
                "MODEL_TEMPERATURE": str(selection.temperature),
            }
        )

    def validate(self, connection: ModelConnection, selection: ModelSelection) -> None:
        self._settings(connection, selection)

    async def generate(
        self, connection: ModelConnection, selection: ModelSelection, request: ChatRequest
    ) -> ChatResult:
        settings = self._settings(connection, selection)
        if connection.secret_ref is not None:
            settings = replace(settings, api_key=await self._secrets.get(connection.secret_ref))
        return await LMStudioChatModel(self._client, settings).generate(request)
