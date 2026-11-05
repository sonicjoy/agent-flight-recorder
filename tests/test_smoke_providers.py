"""Real calls to each provider. Run with: uv run pytest -m smoke

Each test is skipped when its provider's key is not set.
"""

import pytest

from flight_recorder.llm import api_key_for, chat_model, parse_spec
from flight_recorder.pricing import cost_usd
from flight_recorder.settings import Settings

PROMPT = "Reply with exactly the word: pong"

SMOKE_MODELS = [
    "anthropic:claude-haiku-5-5",
    "google_genai:gemini-3.5-flash-lite",
    "openai:gpt-5.4-nano",
]


@pytest.mark.smoke
@pytest.mark.parametrize("spec", SMOKE_MODELS)
def test_provider_answers_same_prompt(spec: str) -> None:
    settings = Settings()
    if api_key_for(parse_spec(spec).provider, settings) is None:
        pytest.skip(f"no key configured for {spec}")

    reply = chat_model(spec, settings=settings).invoke(PROMPT)

    assert "pong" in reply.text.lower()
    usage = reply.usage_metadata
    assert usage is not None and usage["input_tokens"] > 0
    assert cost_usd(spec, usage["input_tokens"], usage["output_tokens"]) is not None
