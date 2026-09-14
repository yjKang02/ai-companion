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


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1234/v1",
        "http://LOCALHOST:1234/v1",
        "http://127.0.0.1:1234/v1",
        "http://127.255.255.254:1234/v1",
        "http://[::1]:1234/v1",
        "http://[0:0:0:0:0:0:0:1]:1234/v1",
        "https://model.example/v1",
        "https://192.0.2.10:1234/v1",
        "https://[2001:db8::1]/v1",
    ],
)
def test_model_endpoint_allows_loopback_http_and_remote_https(url):
    settings = ModelSettings.from_env({"LMSTUDIO_MODEL": "test", "LMSTUDIO_BASE_URL": url})
    assert settings.base_url == url


@pytest.mark.parametrize("require_model", [True, False])
@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.10/v1",
        "http://192.168.1.10:1234/v1",
        "http://10.0.0.1:1234/v1",
        "http://0.0.0.0:1234/v1",
        "http://[::]:1234/v1",
        "http://[2001:db8::1]/v1",
        "http://model.example/v1",
        "http://localhost.example/v1",
        "http://127.0.0.1.example/v1",
        "http://127.1/v1",
        "http://2130706433/v1",
    ],
)
def test_remote_or_ambiguous_http_is_rejected_for_run_and_probe(url, require_model):
    env = {"LMSTUDIO_BASE_URL": url, "LMSTUDIO_API_KEY": "secret-key"}
    if require_model:
        env["LMSTUDIO_MODEL"] = "test"
    with pytest.raises(ConfigurationError, match="HTTPS") as error:
        ModelSettings.from_env(env, require_model=require_model)
    assert "secret-key" not in str(error.value)
    assert url not in str(error.value)
