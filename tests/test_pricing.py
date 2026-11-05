import pytest

from flight_recorder.pricing import cost_usd, price_table
from flight_recorder.settings import Settings


def test_sonnet_cost() -> None:
    # $2 per 1M in, $10 per 1M out
    assert cost_usd("anthropic:claude-sonnet-5-5", 1_000, 500) == pytest.approx(0.007)


def test_haiku_long_context_rate_applies_above_threshold() -> None:
    assert cost_usd("anthropic:claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)
    assert cost_usd("anthropic:claude-haiku-5-5", 100_001, 0) == pytest.approx(0.0500005)


def test_openai_gpt_5_5_long_context_rate() -> None:
    assert cost_usd("openai:gpt-5.5", 272_000, 1_000) == pytest.approx(1.39)
    assert cost_usd("openai:gpt-5.5", 272_001, 1_000) == pytest.approx(2.76501)


def test_gemini_free_tier_costs_nothing() -> None:
    assert cost_usd("google_genai:gemini-3.5-flash-lite", 10_000, 10_000) == 0.0


def test_unknown_model_cost_is_none_not_zero() -> None:
    assert cost_usd("anthropic:claude-unknown", 1_000, 1_000) is None


def test_default_models_have_prices(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    for spec in (settings.llm_model, settings.llm_cheap_model):
        assert spec in price_table().models, f"{spec} missing from pricing.yaml"
