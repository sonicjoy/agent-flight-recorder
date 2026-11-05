import re
from pathlib import Path

import pytest

from flight_recorder.settings import Settings, env_var_names

ENV_EXAMPLE = Path(__file__).parents[1] / ".env.example"


def test_defaults_use_anthropic(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert settings.llm_model == "anthropic:claude-sonnet-5-5"
    assert settings.llm_cheap_model == "anthropic:claude-haiku-5-5"
    assert settings.anthropic_api_key is None


def test_environment_overrides_defaults(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("AFR_LLM_MODEL", "google_genai:gemini-3.8-flash")
    clean_env.setenv("GOOGLE_API_KEY", "test-google-key")

    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert settings.llm_model == "google_genai:gemini-3.8-flash"
    assert settings.google_api_key is not None
    assert settings.google_api_key.get_secret_value() == "test-google-key"


def test_keys_are_hidden_when_printed(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-should-not-appear")

    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert "sk-should-not-appear" not in repr(settings)
    assert "sk-should-not-appear" not in str(settings.model_dump())


def test_env_example_documents_every_setting() -> None:
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.M))

    missing = env_var_names() - documented

    assert not missing, f"Add these to .env.example: {sorted(missing)}"
