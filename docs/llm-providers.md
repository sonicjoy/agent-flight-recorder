# LLM providers

Every agent and the LLM judge get their model from one setting, so the same code runs on any supported provider.

## Choosing a model

A model is named by a spec, `<provider>:<model>`:

| Setting | Default | Used for |
|---|---|---|
| `AFR_LLM_MODEL` | `anthropic:claude-sonnet-5-5` | Agents |
| `AFR_LLM_CHEAP_MODEL` | `anthropic:claude-haiku-5-5` | LLM judge, bulk work |

Provider names are LangChain's, so a spec maps straight onto `init_chat_model`:

| Provider | Key variable | Role in this project |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | Local development and bulk runs. The key stays in the local `.env` only. |
| `google_genai` | `GOOGLE_API_KEY` | Gemini free tier: evals in CI and the second provider in the comparison. |
| `openai` | `OPENAI_API_KEY` | Optional third provider (paid, personal key); not a default. |

In code:

```python
from flight_recorder.llm import chat_model

llm = chat_model()  # AFR_LLM_MODEL
judge = chat_model(cheap=True)  # AFR_LLM_CHEAP_MODEL
gemini = chat_model("google_genai:gemini-3.5-flash-lite")
```

To check a provider by hand:

```bash
uv run afr llm-ping --cheap
uv run afr llm-ping --model google_genai:gemini-3.5-flash-lite
```

It prints the reply, token counts, cost and latency. The smoke tests make the same kind of call on each provider and skip any provider without a key:

```bash
uv run pytest -m smoke -v -rs
```

## Prices

`src/flight_recorder/pricing.yaml` holds USD per 1M tokens. A model missing from it has an unknown cost, reported as `None`, never as $0.

| Model | Input | Output | Source, checked |
|---|---|---|---|
| `anthropic:claude-opus-5-5` | $4.00 | $20.00 | Anthropic API reference, 5 Nov 2026 |
| `anthropic:claude-sonnet-5-5` | $2.00 | $10.00 | Anthropic API reference, 5 Nov 2026 |
| `anthropic:claude-haiku-5-5` | $0.10 ($0.50 above 100K input tokens) | $0.50 ($2.50 above 100K) | Anthropic API reference, 5 Nov 2026 |
| `anthropic:claude-fable-5-1` | $10.00 | $50.00 | platform.claude.com/docs/en/about-claude/pricing, 11 Nov 2026 |
| `openai:gpt-5.4-nano` | $0.20 | $1.25 | [OpenAI pricing](https://developers.openai.com/api/docs/pricing), 5 Nov 2026 |
| `openai:gpt-5.4-mini` | $0.75 | $4.50 | [OpenAI pricing](https://developers.openai.com/api/docs/pricing), 5 Nov 2026 |
| `openai:gpt-5.5` | $5.00 ($10.00 above 272K input tokens) | $30.00 ($45.00 above 272K) | [OpenAI pricing](https://developers.openai.com/api/docs/pricing), 5 Nov 2026 |
| `google_genai:gemini-3.8-flash` | free tier | free tier | [AI Studio pricing](https://aistudio.google.com/docs/pricing), 5 Nov 2026 |
| `google_genai:gemini-3.5-flash-lite` | free tier | free tier | [AI Studio pricing](https://aistudio.google.com/docs/pricing), 5 Nov 2026 |

Claude cache prices per 1M tokens (platform.claude.com/docs/en/about-claude/pricing, checked 11 Nov 2026):

| Model | Cache read | 5-minute write | 1-hour write |
|---|---|---|---|
| Opus 5.5 | $0.20 (0.05x) | $5.00 | $8.00 |
| Sonnet 5.5 | $0.10 (0.05x) | $2.50 | $4.00 |
| Haiku 5.5 | $0.01 ($0.05 above 100K) | $0.125 ($0.625) | $0.20 ($1.00) |
| Fable 5.1 | $0.25 (0.025x) | $12.50 | $20.00 |

Opus 5.5 fast mode is $8 input / $40 output, and stacks with the cache multipliers. Recorded Claude Code sessions are priced at these API rates: on a subscription, that's the API-equivalent cost, not what was paid.

Not yet modelled: prompt-cache read and write prices.

## Gemini free tier

- Get a key from a personal Google account at [Google AI Studio](https://aistudio.google.com/).
- Google uses free-tier inputs to improve its products. That's acceptable here because the triager only sees this repo's own issues. Recorded Claude Code sessions should go to the Anthropic model, not Gemini.
- Rate limits apply per Google Cloud project, not per key, and daily quotas reset at midnight Pacific time. The exact numbers are only shown in your account, under [AI Studio → Rate limits](https://aistudio.google.com/rate-limit). They decide how large the CI eval suite can be.
  - Observed 6 Nov 2026 (from a 429 response): `gemini-3.5-flash-lite` allows **15 requests per minute** on the free tier. One triager run makes about 5-10 model calls, so batch runs (#13) must throttle to about one run every 40 seconds per model. Daily limits not yet recorded.

## Why not GitHub Models

The original plan used GitHub Models for CI, because Actions could call it with the workflow's own token. GitHub retired it on 30 July 2026. On 5 Nov 2026, `models.github.ai/inference/chat/completions` returned `200 text/plain "OK"` instead of a model reply, even with a valid token.
