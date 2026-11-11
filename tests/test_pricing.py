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


def test_cache_reads_and_writes_have_their_own_rates() -> None:
    # Opus 5.5: $4 input, $0.20 cache read, $5 / $8 cache writes, $20 output
    cost = cost_usd(
        "anthropic:claude-opus-5-5", 1_000, 100,
        cache_read=100_000, cache_write_5m=2_000, cache_write_1h=1_000,
    )  # fmt: skip
    assert cost == pytest.approx(
        (1_000 * 4 + 100 * 20 + 100_000 * 0.20 + 2_000 * 5 + 1_000 * 8) / 1e6
    )


def test_haiku_long_context_threshold_counts_cached_tokens() -> None:
    short = cost_usd("anthropic:claude-haiku-5-5", 1_000, 0, cache_read=99_000)
    long = cost_usd("anthropic:claude-haiku-5-5", 1_000, 0, cache_read=99_001)

    assert short == pytest.approx((1_000 * 0.10 + 99_000 * 0.01) / 1e6)
    assert long == pytest.approx((1_000 * 0.50 + 99_001 * 0.05) / 1e6)


def test_fast_mode_rates_and_unknown_fast_mode() -> None:
    assert cost_usd("anthropic:claude-opus-5-5", 1_000, 1_000, fast=True) == pytest.approx(0.048)
    assert cost_usd("anthropic:claude-sonnet-5-5", 1_000, 1_000, fast=True) is None


def test_model_without_cache_rates_prices_cache_at_base_input() -> None:
    assert cost_usd("openai:gpt-5.4-mini", 0, 0, cache_read=1_000_000) == pytest.approx(0.75)
