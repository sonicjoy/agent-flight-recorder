"""Token prices per model, for cost per run.

`input_tokens` is uncached input. Cache reads and writes are priced at their own rates
where pricing.yaml has them; a model without cache rates prices them at the base input
rate, which overstates cost rather than hiding it. These are API list prices: for a
Claude Code subscription the result is the API-equivalent cost, not what was paid.
"""

from functools import cache
from importlib.resources import files

import yaml
from pydantic import BaseModel, NonNegativeFloat, NonNegativeInt


class Rates(BaseModel):
    input: NonNegativeFloat
    output: NonNegativeFloat
    cache_read: NonNegativeFloat | None = None
    cache_write_5m: NonNegativeFloat | None = None
    cache_write_1h: NonNegativeFloat | None = None


class LongContextRates(Rates):
    above_input_tokens: NonNegativeInt


class ModelPrice(Rates):
    long_context: LongContextRates | None = None
    fast: Rates | None = None  # Claude fast mode: a premium that stacks with cache multipliers

    def rates_for(self, prompt_tokens: int) -> Rates:
        """The tier for a request whose prompt (all input, cached or not) has this size."""
        lc = self.long_context
        return lc if lc is not None and prompt_tokens > lc.above_input_tokens else self


class PriceTable(BaseModel):
    version: str
    """The date the prices were last changed; stored on each span it prices."""
    models: dict[str, ModelPrice]


@cache
def price_table() -> PriceTable:
    text = files("flight_recorder").joinpath("pricing.yaml").read_text(encoding="utf-8")
    return PriceTable.model_validate(yaml.safe_load(text))


def cost_usd(
    spec: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    fast: bool = False,
) -> float | None:
    """Cost of one call in US dollars, or None if the model (or its fast mode) has no price."""
    price = price_table().models.get(spec.strip())
    if price is None:
        return None
    if fast:
        if price.fast is None:
            return None
        rates = price.fast
    else:
        rates = price.rates_for(input_tokens + cache_read + cache_write_5m + cache_write_1h)
    per_token = [
        (input_tokens, rates.input),
        (output_tokens, rates.output),
        (cache_read, rates.cache_read),
        (cache_write_5m, rates.cache_write_5m),
        (cache_write_1h, rates.cache_write_1h),
    ]
    return sum(n * (rate if rate is not None else rates.input) for n, rate in per_token) / 1e6
