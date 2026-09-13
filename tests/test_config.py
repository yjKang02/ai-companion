import pytest

from ai_companion.config import (
    ConfigurationError,
    ConversationSettings,
    DiscordSettings,
    ModelSettings,
)


def test_model_probe_does_not_require_discord_or_model_id():
    settings = ModelSettings.from_env({}, require_model=False)
    assert settings.base_url == "http://127.0.0.1:1234/v1"
    assert settings.model == ""


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DISCORD_TOKEN": "secret"},
        {"DISCORD_TOKEN": "secret", "DISCORD_ALLOWED_USER_IDS": "123,abc"},
        {"DISCORD_TOKEN": "secret", "DISCORD_ALLOWED_USER_IDS": "0"},
        {"DISCORD_TOKEN": "secret", "DISCORD_ALLOWED_USER_IDS": "123,"},
    ],
)
def test_discord_missing_or_invalid_allowlist_fails_closed(env):
    with pytest.raises(ConfigurationError) as error:
        DiscordSettings.from_env(env)
    assert "secret" not in str(error.value)


def test_secrets_are_not_in_settings_repr():
    discord = DiscordSettings.from_env(
        {"DISCORD_TOKEN": "secret-token", "DISCORD_ALLOWED_USER_IDS": "123,456"}
    )
    model = ModelSettings.from_env({"LMSTUDIO_MODEL": "test", "LMSTUDIO_API_KEY": "secret-key"})
    assert "secret-token" not in repr(discord)
    assert "secret-key" not in repr(model)
    assert discord.allowed_user_ids == frozenset({123, 456})


@pytest.mark.parametrize(
    "overrides",
    [
        {"MODEL_PROVIDER": "unsupported"},
        {"MODEL_TIMEOUT_SECONDS": "nan"},
        {"MODEL_MAX_TOKENS": "0"},
        {"MODEL_TEMPERATURE": "inf"},
        {"LMSTUDIO_BASE_URL": "file:///tmp/v1"},
        {"LMSTUDIO_BASE_URL": "http://user:secret@localhost/v1"},
        {"LMSTUDIO_BASE_URL": "http://localhost/v1?key=secret"},
        {"LMSTUDIO_BASE_URL": "http://localhost:not-port/v1"},
    ],
)
def test_invalid_model_settings_are_rejected(overrides):
    with pytest.raises(ConfigurationError):
        ModelSettings.from_env({"LMSTUDIO_MODEL": "test", **overrides})


def test_odd_history_limit_is_rejected():
    with pytest.raises(ConfigurationError):
        ConversationSettings.from_env({"CONVERSATION_HISTORY_MESSAGES": "3"})
