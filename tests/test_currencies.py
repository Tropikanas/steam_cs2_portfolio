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


def test_steam_rates_view(conn):
    from steamfolio import views

    assert views.steam_rates(conn) == {"taken_at": None, "rates": [], "items": []}
    knife = "★ Karambit | Doppler (Factory New)"
    for run_id, at, rub in ((1, "2026-10-30T06:00:00+00:00", 80.0), (2, "2026-10-31T06:00:00+00:00", 82.0)):
        with conn:
            conn.execute("INSERT INTO collector_runs (id, started_at, finished_at, status) VALUES (?, ?, ?, 'ok')", (run_id, at, at))
            conn.execute("INSERT INTO fx_snapshots VALUES (?, ?, ?, ?, 5, 0.5)", (run_id, at, int(Currency.RUB), rub))
            conn.execute("INSERT INTO fx_snapshots VALUES (?, ?, ?, ?, 5, 0.5)", (run_id, at, int(Currency.KZT), 500.0))
            conn.executemany("INSERT INTO fx_samples VALUES (?, ?, ?, ?)",
                             [(run_id, knife, int(Currency.USD), 150000), (run_id, knife, int(Currency.RUB), int(150000 * rub))])
    data = views.steam_rates(conn)
    assert data["taken_at"] == "2026-10-31T06:00:00+00:00"
    assert [(r["code"], r["rate"], r["pct_1d"]) for r in data["rates"]] == [("RUB", 82.0, 2.5), ("KZT", 500.0, 0.0)]
    assert data["items"][0]["prices"] == {"USD": 1500.0, "RUB": 123000.0}


def test_chart_currencies_sorted_by_invested(journal, acc):
    journal.buy(CASE, acc, Currency.RUB, 1, 8000, D("2026-10-01"))  # $1
    journal.buy(CASE, acc, Currency.USD, 1, 300, D("2026-10-01"))  # $3
    journal.buy("AK", acc, Currency.UAH, 1, 4000, D("2026-10-01"))  # $1
    journal.sell("AK", acc, Currency.UAH, 1, 4000, D("2026-10-02"))  # продано — в списке, но последней
    journal.buy("AWP", acc, Currency.CNY, 1, 700, D("2026-10-01"))
    journal.remove_position("AWP", acc, Currency.CNY)  # удалено как ошибочное — валюты нет в списке
    codes = [c["code"] for c in stats.chart_currencies(replay(journal.ops()))]
    assert codes == ["USD", "RUB", "UAH"]
