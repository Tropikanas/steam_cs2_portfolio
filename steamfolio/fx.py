"""Курс валюты к USD на дату операции.

С начала истории Steam — курс Steam из ближайшего снимка не позже этой даты.
Для более ранних дат — курс ЦБ РФ на эту дату, пересчитанный к USD через рубль.
"""

import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import httpx

from steamfolio.clock import msk_date, msk_day_end_utc
from steamfolio.currencies import Currency

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"


class FxUnavailable(Exception):
    """Курс не найден — нужно ввести вручную."""


def parse_cbr(xml: bytes) -> dict[Currency, Decimal]:
    """XML ЦБ (рублей за nominal единиц) → единиц валюты за 1 USD."""
    rub_per_unit: dict[str, Decimal] = {}
    for valute in ET.fromstring(xml).iter("Valute"):
        code = valute.findtext("CharCode")
        value = Decimal(valute.findtext("Value", "0").replace(",", "."))
        nominal = Decimal(valute.findtext("Nominal", "1"))
        rub_per_unit[code] = value / nominal
    usd = rub_per_unit.get("USD")
    if not usd:
        raise FxUnavailable("в ответе ЦБ нет курса доллара")
    rates = {Currency.RUB: usd}
    for currency in (Currency.CNY, Currency.UAH):
        if currency.name in rub_per_unit:
            rates[currency] = usd / rub_per_unit[currency.name]
    return rates


def fetch_cbr(day: date) -> dict[Currency, Decimal]:
    response = httpx.get(CBR_URL, params={"date_req": day.strftime("%d/%m/%Y")}, timeout=30)
    response.raise_for_status()
    return parse_cbr(response.content)


class FxResolver:
    def __init__(self, conn: sqlite3.Connection, cbr_fetch: Callable[[date], dict[Currency, Decimal]] = fetch_cbr):
        self._conn = conn
        self._cbr_fetch = cbr_fetch

    def rate(self, currency: Currency, day: date) -> tuple[Decimal, str]:
        """(единиц валюты за 1 USD, источник: usd | steam | cbr)."""
        if currency == Currency.USD:
            return Decimal(1), "usd"
        first = self._conn.execute(
            "SELECT MIN(taken_at) FROM fx_snapshots WHERE currency = ?", (int(currency),)
        ).fetchone()[0]
        if first is not None and day >= msk_date(first):
            row = self._conn.execute(
                """SELECT rate FROM fx_snapshots WHERE currency = ? AND taken_at < ?
                   ORDER BY taken_at DESC LIMIT 1""",
                (int(currency), msk_day_end_utc(day)),
            ).fetchone()
            return Decimal(str(row[0])), "steam"
        return self._cbr(currency, day), "cbr"

    def _cbr(self, currency: Currency, day: date) -> Decimal:
        row = self._conn.execute(
            "SELECT rate FROM fx_cbr WHERE date = ? AND currency = ?", (day.isoformat(), int(currency))
        ).fetchone()
        if row:
            return Decimal(row[0])
        try:
            rates = self._cbr_fetch(day)
        except (httpx.HTTPError, ET.ParseError) as e:
            raise FxUnavailable(f"ЦБ не ответил: {e!r}") from e
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO fx_cbr (date, currency, rate) VALUES (?, ?, ?)",
                [(day.isoformat(), int(c), str(r)) for c, r in rates.items()],
            )
        if currency not in rates:
            raise FxUnavailable(f"у ЦБ нет курса {currency.name} на {day:%d.%m.%Y}")
        return rates[currency]
