import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class ConfigurationError(Exception):
    pass


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigurationError(f"{key} 설정이 필요합니다.")
    return value


def _integer(env: Mapping[str, str], key: str, default: str, low: int, high: int) -> int:
    try:
        value = int(env.get(key, default))
        if low <= value <= high:
            return value
    except ValueError:
        pass
    raise ConfigurationError(f"{key}는 {low}~{high} 정수여야 합니다.")


def _number(env: Mapping[str, str], key: str, default: str, low: float, high: float) -> float:
    try:
        value = float(env.get(key, default))
        if math.isfinite(value) and low <= value <= high:
            return value
    except ValueError:
        pass
    raise ConfigurationError(f"{key}는 {low}~{high} 숫자여야 합니다.")


@dataclass(frozen=True, slots=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout: float
    max_tokens: int
    temperature: float

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, require_model: bool = True) -> "ModelSettings":
        if env.get("MODEL_PROVIDER", "lmstudio").strip() != "lmstudio":
            raise ConfigurationError("현재 MODEL_PROVIDER는 lmstudio만 지원합니다.")
        base_url = env.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").strip().rstrip("/")
        try:
            parts = urlsplit(base_url)
            valid = (
                parts.scheme in {"http", "https"}
                and parts.hostname
                and not parts.username
                and not parts.password
                and not parts.query
                and not parts.fragment
                and parts.path == "/v1"
            )
            _ = parts.port
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError("LMSTUDIO_BASE_URL은 /v1로 끝나는 HTTP(S) 주소여야 합니다.")
        model = (
            _required(env, "LMSTUDIO_MODEL")
            if require_model
            else env.get("LMSTUDIO_MODEL", "").strip()
        )
        return cls(
            base_url=base_url,
            model=model,
            api_key=env.get("LMSTUDIO_API_KEY", "").strip(),
            timeout=_number(env, "MODEL_TIMEOUT_SECONDS", "60", 1, 600),
            max_tokens=_integer(env, "MODEL_MAX_TOKENS", "256", 1, 8192),
            temperature=_number(env, "MODEL_TEMPERATURE", "0.7", 0, 2),
        )


@dataclass(frozen=True, slots=True)
class DiscordSettings:
    token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    queue_size: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "DiscordSettings":
        token = _required(env, "DISCORD_TOKEN")
        raw_ids = _required(env, "DISCORD_ALLOWED_USER_IDS")
        try:
            parts = [part.strip() for part in raw_ids.split(",")]
            if not all(part.isascii() and part.isdigit() for part in parts):
                raise ValueError
            ids = frozenset(int(part) for part in parts)
            if any(value <= 0 for value in ids):
                raise ValueError
        except ValueError:
            raise ConfigurationError(
                "DISCORD_ALLOWED_USER_IDS는 양의 정수 ID 목록이어야 합니다."
            ) from None
        return cls(token, ids, _integer(env, "DISCORD_QUEUE_SIZE", "32", 1, 256))


@dataclass(frozen=True, slots=True)
class ConversationSettings:
    system_prompt: str
    history_messages: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ConversationSettings":
        prompt = env.get(
            "COMPANION_SYSTEM_PROMPT",
            "너는 일상 대화를 나누는 AI 친구야. 한국어로 자연스럽게 한두 문장으로 답해. "
            "모르는 개인 정보는 지어내지 마.",
        ).strip()
        if not prompt or len(prompt) > 8000:
            raise ConfigurationError("COMPANION_SYSTEM_PROMPT는 1~8000자여야 합니다.")
        count = _integer(env, "CONVERSATION_HISTORY_MESSAGES", "20", 2, 100)
        if count % 2:
            raise ConfigurationError("CONVERSATION_HISTORY_MESSAGES는 짝수여야 합니다.")
        return cls(prompt, count)
