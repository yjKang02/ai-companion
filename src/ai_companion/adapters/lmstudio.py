import asyncio
from typing import Any

import httpx

from ai_companion.application.ports import (
    InvalidModelOutput,
    ModelAuthenticationError,
    ModelRateLimited,
    ModelTimeout,
    ModelUnavailable,
)
from ai_companion.config import ModelSettings
from ai_companion.domain import ChatRequest, ChatResult, TokenUsage


class LMStudioChatModel:
    """LM Studio의 OpenAI 호환 HTTP 응답을 공통 결과로 변환한다."""

    def __init__(self, client: httpx.AsyncClient, settings: ModelSettings) -> None:
        self._client = client
        self._settings = settings

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        try:
            # HTTPX의 구간별 timeout 외에 전체 요청 제한도 적용한다.
            async with asyncio.timeout(self._settings.timeout):
                response = await self._client.request(
                    method,
                    f"{self._settings.base_url}/{path}",
                    headers=headers,
                    timeout=self._settings.timeout,
                    **kwargs,
                )
        except (httpx.TimeoutException, TimeoutError):
            raise ModelTimeout("모델 응답 시간 초과") from None
        except httpx.RequestError:
            raise ModelUnavailable("모델 서버에 연결할 수 없습니다.") from None
        if response.status_code in {401, 403}:
            raise ModelAuthenticationError("LM Studio 인증 설정을 확인하세요.")
        if response.status_code == 429:
            raise ModelRateLimited("모델 서버 요청 한도 초과")
        if not 200 <= response.status_code < 300:
            raise ModelUnavailable(f"모델 서버 HTTP 상태: {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise InvalidModelOutput("모델 서버가 JSON을 반환하지 않았습니다.") from None

    async def list_models(self) -> tuple[str, ...]:
        body = await self._request("GET", "models")
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise InvalidModelOutput("모델 목록 형식이 올바르지 않습니다.")
        ids: list[str] = []
        for item in body["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise InvalidModelOutput("모델 ID 형식이 올바르지 않습니다.")
            ids.append(item["id"])
        return tuple(ids)

    async def generate(self, request: ChatRequest) -> ChatResult:
        if not self._settings.model:
            raise ModelUnavailable("LMSTUDIO_MODEL 설정이 필요합니다.")
        body = await self._request(
            "POST",
            "chat/completions",
            json={
                "model": self._settings.model,
                "messages": [
                    {"role": message.role.value, "content": message.content}
                    for message in request.messages
                ],
                "stream": False,
                "max_tokens": self._settings.max_tokens,
                "temperature": self._settings.temperature,
            },
        )
        try:
            choice = body["choices"][0]
            message = choice["message"]
            text = message["content"]
            reason = choice.get("finish_reason")
            if (
                message.get("role") != "assistant"
                or message.get("tool_calls")
                or not isinstance(text, str)
                or not text.strip()
                or (reason is not None and not isinstance(reason, str))
                or reason in {"tool_calls", "content_filter"}
            ):
                raise ValueError
            # 별도 reasoning 필드는 사용하지 않는다. 본문에 섞인 추론 태그도 보내지 않는다.
            if "<think" in text.lower() or "</think" in text.lower():
                raise ValueError
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise InvalidModelOutput("지원하는 assistant 텍스트 응답이 아닙니다.") from None
        usage = body.get("usage")
        counts = None
        if isinstance(usage, dict):
            counts = TokenUsage(
                input_tokens=self._token_count(usage.get("prompt_tokens")),
                output_tokens=self._token_count(usage.get("completion_tokens")),
            )
        return ChatResult(text=text.strip(), finish_reason=reason, usage=counts)

    @staticmethod
    def _token_count(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None
