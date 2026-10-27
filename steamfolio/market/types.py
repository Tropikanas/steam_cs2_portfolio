from dataclasses import dataclass

from steamfolio.currencies import Currency


class MarketError(Exception):
    """Steam не отдал данные (сеть, лимит запросов, неожиданный ответ)."""


@dataclass(frozen=True)
class Quote:
    market_hash_name: str
    currency: Currency
    best_buy: int | None  # лучший ордер на покупку, в сотых долях; None — ордеров нет
    lowest_sell: int | None  # самый дешёвый лот на продажу
    source: str  # 'histogram' | 'orderbook'
