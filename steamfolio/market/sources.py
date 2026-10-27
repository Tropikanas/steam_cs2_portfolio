"""Два способа получить стакан предмета.

histogram — старый API itemordershistogram. Валюта задаётся явно, но нужен item_nameid,
            который Steam больше нигде не показывает (берём из открытого справочника).
orderbook  — внутренний запрос новых страниц Market Beta. Работает по названию,
            но валюту Steam выбирает по стране IP (для сервера в Нидерландах — EUR).
"""

import json
from typing import Any

from steamfolio.currencies import Currency
from steamfolio.market.http import SteamHttp
from steamfolio.market.types import MarketError, Quote

APP_ID = 730
HISTOGRAM_URL = "https://steamcommunity.com/market/itemordershistogram"
ORDERBOOK_URL = "https://steamcommunity.com/market/orderbook"
ACTIONS_URL = "https://steamcommunity.com/market/actions"


def _minor(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def fetch_histogram(http: SteamHttp, name: str, item_nameid: int, currency: Currency) -> Quote:
    data = http.get_json(
        HISTOGRAM_URL,
        {
            "country": "US",
            "language": "english",
            "currency": int(currency),
            "item_nameid": item_nameid,
            "two_factor": 0,
        },
    )
    if not isinstance(data, dict) or data.get("success") != 1:
        raise MarketError(f"histogram: неуспешный ответ для {name!r}: {str(data)[:200]}")
    return Quote(name, currency, _minor(data.get("highest_buy_order")), _minor(data.get("lowest_sell_order")), "histogram")


def fetch_price_history(http: SteamHttp, name: str) -> tuple[Currency, list[tuple[int, float, int]]]:
    """История продаж Market Beta: [(unix-время, медианная цена в единицах валюты, число продаж)].

    Точки дневные, за последний месяц — почасовые. Валюту Steam выбирает по стране IP (EUR).
    """
    data = http.get_json(
        ACTIONS_URL,
        {"q": "QueryPriceHistory", "qp": json.dumps([APP_ID, name], ensure_ascii=False)},
        headers={"x-valve-request-type": "queryAction"},
    )
    try:
        body = data["data"]
        currency = Currency(body["ecurrency"])
        points = [(int(p["time"]), float(p["price_median"]), int(p.get("purchases") or 0)) for p in body["prices"]]
    except (KeyError, TypeError) as e:
        raise MarketError(f"pricehistory: неожиданный формат ответа для {name!r}: {e!r}") from e
    except ValueError as e:
        raise MarketError(f"pricehistory: неизвестная валюта для {name!r}") from e
    return currency, points


def fetch_orderbook(http: SteamHttp, name: str) -> Quote:
    data = http.get_json(
        ORDERBOOK_URL,
        {"q": "Load", "qp": json.dumps([APP_ID, name], ensure_ascii=False)},
        headers={"x-valve-request-type": "queryAction"},
    )
    try:
        book = data["data"]
        if not book.get("success"):
            raise MarketError(f"orderbook: неуспешный ответ для {name!r}")
        book = book["data"]
        currency = Currency(book["eCurrency"])
    except (KeyError, TypeError) as e:
        raise MarketError(f"orderbook: неожиданный формат ответа для {name!r}: {e!r}") from e
    except ValueError as e:
        raise MarketError(f"orderbook: Steam ответил в неизвестной валюте {book.get('eCurrency')}") from e
    return Quote(name, currency, _minor(book.get("amtMaxBuyOrder")), _minor(book.get("amtMinSellOrder")), "orderbook")
