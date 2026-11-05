import pytest
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from flight_recorder.llm import (
    MissingCredentialsError,
    ModelSpec,
    ModelSpecError,
    chat_model,
    parse_spec,
)
from flight_recorder.settings import Settings


def make_settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_parse_spec_splits_provider_and_model() -> None:
    assert parse_spec("anthropic:claude-sonnet-5-5") == ModelSpec("anthropic", "claude-sonnet-5-5")
    assert str(parse_spec(" google_genai:gemini-3.8-flash ")) == "google_genai:gemini-3.8-flash"


@pytest.mark.parametrize("bad", ["claude-sonnet-5-5", "anthropic:", ":model", ""])
def test_parse_spec_rejects_malformed(bad: str) -> None:
    with pytest.raises(ModelSpecError, match="<provider>:<model>"):
        parse_spec(bad)


def test_parse_spec_rejects_unsupported_provider() -> None:
    with pytest.raises(ModelSpecError, match="Unsupported provider 'github'"):
        parse_spec("github:openai/gpt-4.1")


def test_missing_key_names_the_variable(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(MissingCredentialsError, match="GOOGLE_API_KEY"):
        chat_model("google_genai:gemini-3.5-flash-lite", settings=make_settings())


def test_builds_anthropic_model(clean_env: pytest.MonkeyPatch) -> None:
    llm = chat_model(settings=make_settings(ANTHROPIC_API_KEY="test-key"))

    assert isinstance(llm, ChatAnthropic)
    assert llm.model == "claude-sonnet-5-5"


def test_cheap_uses_cheap_model(clean_env: pytest.MonkeyPatch) -> None:
    llm = chat_model(cheap=True, settings=make_settings(ANTHROPIC_API_KEY="test-key"))

    assert isinstance(llm, ChatAnthropic)
    assert llm.model == "claude-haiku-5-5"


def test_builds_openai_model(clean_env: pytest.MonkeyPatch) -> None:
    llm = chat_model("openai:gpt-5.4-nano", settings=make_settings(OPENAI_API_KEY="test-key"))

    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "gpt-5.4-nano"


def test_builds_gemini_model_from_config(clean_env: pytest.MonkeyPatch) -> None:
    settings = make_settings(
        AFR_LLM_MODEL="google_genai:gemini-3.8-flash", GOOGLE_API_KEY="test-key"
    )

    llm = chat_model(settings=settings)

    assert isinstance(llm, ChatGoogleGenerativeAI)
    assert llm.model.endswith("gemini-3.8-flash")
