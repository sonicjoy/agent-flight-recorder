"""Provider-switchable chat models.

A model is named by a spec string, `<provider>:<model>`, for example
`anthropic:claude-sonnet-5-5` or `google_genai:gemini-3.5-flash-lite`.
Provider names are LangChain's, so the spec maps straight onto `init_chat_model`.
"""

from dataclasses import dataclass

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from flight_recorder.settings import Settings

# provider -> (Settings attribute holding its key, environment variable to set)
PROVIDERS: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "google_genai": ("google_api_key", "GOOGLE_API_KEY"),
    "openai": ("openai_api_key", "OPENAI_API_KEY"),
}


class ModelSpecError(ValueError):
    """The model spec is malformed or names an unsupported provider."""


class MissingCredentialsError(RuntimeError):
    """No API key is configured for the requested provider."""


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_spec(spec: str) -> ModelSpec:
    provider, sep, model = spec.strip().partition(":")
    if not sep or not provider or not model:
        raise ModelSpecError(f"Model spec must be '<provider>:<model>', got {spec!r}")
    if provider not in PROVIDERS:
        supported = ", ".join(sorted(PROVIDERS))
        raise ModelSpecError(f"Unsupported provider {provider!r}; supported: {supported}")
    return ModelSpec(provider, model)


def api_key_for(provider: str, settings: Settings) -> SecretStr | None:
    attribute, _ = PROVIDERS[provider]
    key = getattr(settings, attribute)
    return key if isinstance(key, SecretStr) and key.get_secret_value() else None


def chat_model(
    spec: str | None = None, *, cheap: bool = False, settings: Settings | None = None
) -> BaseChatModel:
    """Build a chat model from a spec, or from the configured default.

    `cheap=True` uses `AFR_LLM_CHEAP_MODEL` when no spec is given.
    """
    settings = settings or Settings()
    parsed = parse_spec(spec or (settings.llm_cheap_model if cheap else settings.llm_model))
    key = api_key_for(parsed.provider, settings)
    if key is None:
        env_var = PROVIDERS[parsed.provider][1]
        raise MissingCredentialsError(f"{parsed} needs {env_var} set in the environment or .env")
    model = init_chat_model(
        parsed.model,
        model_provider=parsed.provider,
        api_key=key.get_secret_value(),
        max_retries=2,
    )
    assert isinstance(model, BaseChatModel)
    return model
