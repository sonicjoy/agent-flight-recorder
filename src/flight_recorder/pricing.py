"""Token prices per model, for cost per run."""

from functools import cache
from importlib.resources import files

import yaml
from pydantic import BaseModel, NonNegativeFloat, NonNegativeInt


class Rates(BaseModel):
    input: NonNegativeFloat
    output: NonNegativeFloat


class LongContextRates(Rates):
    above_input_tokens: NonNegativeInt


class ModelPrice(Rates):
    long_context: LongContextRates | None = None

    def rates_for(self, input_tokens: int) -> Rates:
        lc = self.long_context
        return lc if lc is not None and input_tokens > lc.above_input_tokens else self


class PriceTable(BaseModel):
    models: dict[str, ModelPrice]


@cache
def price_table() -> PriceTable:
    text = files("flight_recorder").joinpath("pricing.yaml").read_text(encoding="utf-8")
    return PriceTable.model_validate(yaml.safe_load(text))


def cost_usd(spec: str, input_tokens: int, output_tokens: int) -> float | None:
    """Cost of one call in US dollars, or None if the model has no known price."""
    price = price_table().models.get(spec.strip())
    if price is None:
        return None
    rates = price.rates_for(input_tokens)
    return (input_tokens * rates.input + output_tokens * rates.output) / 1_000_000
