"""Settings read from environment variables and a local `.env` file.

Every setting here must also be documented in `.env.example` (enforced by a test).
"""

from pathlib import Path

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
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+psycopg://afr:afr@127.0.0.1:5433/afr"),
        validation_alias=AliasChoices("AFR_DATABASE_URL"),
        description="SQLAlchemy URL of the run store; default: compose.yaml's Postgres.",
    )
    redaction_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AFR_REDACTION_KEY"),
        description="Key for redaction fingerprints; unset means <AFR_HOME>/redaction.key.",
    )
    afr_home: Path = Field(
        default_factory=lambda: Path.home() / ".afr",
        validation_alias=AliasChoices("AFR_HOME"),
        description="User-level folder for the key and the hook's files; set it in the "
        "environment, not .env, so the hook (which reads only the environment) agrees.",
    )


def env_var_names() -> set[str]:
    """Environment variable names that Settings reads."""
    names: set[str] = set()
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names.update(str(choice) for choice in alias.choices)
    return names
