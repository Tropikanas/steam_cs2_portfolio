from datetime import date
from decimal import Decimal

from steamfolio import collector, stats
from steamfolio.currencies import NAMES, SYMBOLS, USER_CURRENCIES, Currency, rate_currencies
from steamfolio.fx import parse_cbr
from steamfolio.ledger import replay

D = date.fromisoformat
CASE = "Kilowatt Case"


def test_all_steam_currencies_described():
    assert len(Currency) == 39
    assert set(SYMBOLS) == set(Currency) == set(NAMES) == set(USER_CURRENCIES)
    assert USER_CURRENCIES[:4] == (Currency.RUB, Currency.USD, Currency.CNY, Currency.UAH)


def test_rate_currencies_add_used():
    assert rate_currencies() == (Currency.RUB, Currency.CNY, Currency.UAH, Currency.EUR)
    assert rate_currencies({Currency.USD, Currency.KZT, Currency.GBP, Currency.RUB})[4:] == (Currency.GBP, Currency.KZT)


def test_buy_in_any_steam_currency_and_collector_targets(conn, journal, fx, acc):
    fx.default[Currency.KZT] = Decimal(500)
    journal.buy(CASE, acc, Currency.KZT, 2, 5100, D("2026-10-01"))  # 51 ₸
    assert journal.ledger().invested_usd == Decimal(2 * 5100) / 500
    (target,) = collector.targets(conn)
    assert target.currencies == (Currency.USD, Currency.KZT)


def test_parse_cbr_takes_every_known_currency():
    xml = """<?xml version="1.0" encoding="windows-1251"?><ValCurs>
      <Valute><CharCode>USD</CharCode><Nominal>1</Nominal><Value>80,0000</Value></Valute>
      <Valute><CharCode>KZT</CharCode><Nominal>100</Nominal><Value>16,0000</Value></Valute>
      <Valute><CharCode>GBP</CharCode><Nominal>1</Nominal><Value>100,0000</Value></Valute>
      <Valute><CharCode>XDR</CharCode><Nominal>1</Nominal><Value>105,0000</Value></Valute>
    </ValCurs>""".encode("cp1251")
    rates = parse_cbr(xml)
    assert rates[Currency.RUB] == 80 and rates[Currency.KZT] == 500 and rates[Currency.GBP] == Decimal("0.8")
    assert Currency.UAH not in rates


def test_chart_currencies_sorted_by_invested(journal, acc):
    journal.buy(CASE, acc, Currency.RUB, 1, 8000, D("2026-10-01"))  # $1
    journal.buy(CASE, acc, Currency.USD, 1, 300, D("2026-10-01"))  # $3
    journal.buy("AK", acc, Currency.UAH, 1, 4000, D("2026-10-01"))  # $1
    journal.sell("AK", acc, Currency.UAH, 1, 4000, D("2026-10-02"))  # продано — в списке, но последней
    journal.buy("AWP", acc, Currency.CNY, 1, 700, D("2026-10-01"))
    journal.remove_position("AWP", acc, Currency.CNY)  # удалено как ошибочное — валюты нет в списке
    codes = [c["code"] for c in stats.chart_currencies(replay(journal.ops()))]
    assert codes == ["USD", "RUB", "UAH"]
