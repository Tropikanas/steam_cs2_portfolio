"""Внутренний курс Steam.

Один и тот же лот Steam показывает в каждой валюте по своему курсу. Цена лота в валюте,
делённая на цену в USD, даёт курс. Берём несколько дорогих предметов и медиану:
на дешёвых предметах округление до сотых искажает курс.
"""

from dataclasses import dataclass
from statistics import median

from steamfolio.currencies import Currency


@dataclass(frozen=True)
class Rate:
    currency: Currency
    rate: float  # единиц валюты за 1 USD
    samples: int
    spread_pct: float  # разброс между предметами: (max − min) / медиана, %


def compute_rates(
    prices: dict[str, dict[Currency, int | None]],
    currencies: tuple[Currency, ...],
    min_usd_minor: int,
    min_samples: int,
) -> tuple[dict[Currency, Rate], list[str]]:
    """prices: предмет → валюта → цена самого дешёвого лота в сотых долях."""
    problems: list[str] = []
    ratios: dict[Currency, list[float]] = {c: [] for c in currencies}
    for name, by_currency in prices.items():
        usd = by_currency.get(Currency.USD)
        if not usd:
            problems.append(f"{name}: нет цены в USD")
            continue
        if usd < min_usd_minor:
            problems.append(f"{name}: дешевле порога (${usd / 100:.2f}), пропущен")
            continue
        for currency in currencies:
            price = by_currency.get(currency)
            if price:
                ratios[currency].append(price / usd)

    rates: dict[Currency, Rate] = {}
    for currency, values in ratios.items():
        if len(values) < min_samples:
            problems.append(f"{currency.name}: только {len(values)} предметов для курса, нужно {min_samples}")
            continue
        mid = median(values)
        rates[currency] = Rate(currency, mid, len(values), (max(values) - min(values)) / mid * 100)
    return rates, problems


def convert(amount: int | None, source: Currency, target: Currency, rates: dict[Currency, float]) -> int | None:
    """Пересчёт суммы в сотых долях через курсы к USD (для USD курс равен 1)."""
    if amount is None:
        return None
    to_usd = 1.0 if source == Currency.USD else rates[source]
    from_usd = 1.0 if target == Currency.USD else rates[target]
    return round(amount / to_usd * from_usd)
