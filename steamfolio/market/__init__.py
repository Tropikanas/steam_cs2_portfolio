"""Сборщик данных торговой площадки Steam.

Всё, что знает о том, как устроена площадка, живёт в этом пакете:
если Valve снова её поменяет, править нужно только здесь.
"""

import logging
import sqlite3

from steamfolio.currencies import Currency
from steamfolio.market.http import SteamHttp
from steamfolio.market.nameids import get_nameid
from steamfolio.market.sources import fetch_histogram, fetch_orderbook
from steamfolio.market.types import MarketError, Quote

__all__ = ["Market", "MarketError", "Quote", "SteamHttp", "listing_url"]

log = logging.getLogger(__name__)


def listing_url(name: str) -> str:
    from urllib.parse import quote

    return f"https://steamcommunity.com/market/listings/730/{quote(name, safe='')}"


class Market:
    def __init__(self, conn: sqlite3.Connection, http: SteamHttp):
        self._conn = conn
        self._http = http
        self._orderbooks: dict[str, Quote] = {}

    def quote(self, name: str, currency: Currency) -> Quote:
        """Стакан предмета в запрошенной валюте.

        Если item_nameid неизвестен или старый API не ответил, возвращается стакан
        Market Beta в той валюте, которую выбрал Steam: quote.currency может отличаться
        от запрошенной, пересчёт — на стороне вызывающего.
        """
        item_nameid = get_nameid(self._conn, name)
        if item_nameid is not None:
            try:
                return fetch_histogram(self._http, name, item_nameid, currency)
            except MarketError as e:
                log.warning("histogram не ответил для %r (%s), пробую orderbook", name, e)
        else:
            log.warning("Нет item_nameid для %r, беру orderbook Market Beta", name)
        if name not in self._orderbooks:
            self._orderbooks[name] = fetch_orderbook(self._http, name)
        return self._orderbooks[name]
