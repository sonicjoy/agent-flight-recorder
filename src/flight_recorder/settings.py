"""Settings read from environment variables and a local `.env` file.

Every setting here must also be documented in `.env.example` (enforced by a test).
"""

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    llm_model: str = Field(
        default="anthropic:claude-sonnet-5-5",
        validation_alias=AliasChoices("AFR_LLM_MODEL"),
        description="Model for agents, as <provider>:<model>.",
    )
    llm_cheap_model: str = Field(
        default="anthropic:claude-haiku-5-5",
        validation_alias=AliasChoices("AFR_LLM_CHEAP_MODEL"),
        description="Cheaper model for the LLM judge and bulk work, as <provider>:<model>.",
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )
    google_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GOOGLE_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY")
    )


def env_var_names() -> set[str]:
    """Environment variable names that Settings reads."""
    names: set[str] = set()
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names.update(str(choice) for choice in alias.choices)
    return names
