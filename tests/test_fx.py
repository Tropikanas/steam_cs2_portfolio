from datetime import date
from decimal import Decimal

import pytest

from steamfolio.currencies import Currency
from steamfolio.fx import FxResolver, FxUnavailable, parse_cbr

# Фрагмент ответа ЦБ на 15.11.2021 (гривна котируется за 10 единиц).
CBR_2021_11_15 = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="13.11.2021" name="Foreign Currency Market">
<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal><Value>71,8118</Value></Valute>
<Valute ID="R01375"><NumCode>156</NumCode><CharCode>CNY</CharCode><Nominal>1</Nominal><Value>11,2369</Value></Valute>
<Valute ID="R01720"><NumCode>980</NumCode><CharCode>UAH</CharCode><Nominal>10</Nominal><Value>27,5194</Value></Valute>
</ValCurs>""".encode("cp1251")


def test_parse_cbr_cross_rates_to_usd():
    rates = parse_cbr(CBR_2021_11_15)
    assert rates[Currency.RUB] == Decimal("71.8118")
    assert round(rates[Currency.CNY], 3) == Decimal("6.391")
    assert round(rates[Currency.UAH], 3) == Decimal("26.095")


def _snapshot(conn, taken_at, rate):
    with conn:
        run = conn.execute("INSERT INTO collector_runs (started_at, status) VALUES (?, 'ok')", (taken_at,)).lastrowid
        conn.execute(
            "INSERT INTO fx_snapshots (run_id, taken_at, currency, rate, samples, spread_pct) VALUES (?, ?, 5, ?, 5, 0)",
            (run, taken_at, rate),
        )


def test_resolver_uses_cbr_before_steam_history_and_caches(conn):
    calls = []

    def fake_cbr(day):
        calls.append(day)
        return parse_cbr(CBR_2021_11_15)

    _snapshot(conn, "2026-10-27T12:30:00+00:00", 80.811)
    fx = FxResolver(conn, fake_cbr)
    assert fx.rate(Currency.RUB, date(2021, 11, 15)) == (Decimal("71.8118"), "cbr")
    fx.rate(Currency.CNY, date(2021, 11, 15))
    assert calls == [date(2021, 11, 15)]  # второй раз — из кэша


def test_resolver_uses_latest_steam_snapshot_not_after_the_day(conn):
    _snapshot(conn, "2026-10-27T12:30:00+00:00", 80.8)
    _snapshot(conn, "2026-10-28T06:00:00+00:00", 81.0)
    _snapshot(conn, "2026-10-28T22:00:00+00:00", 82.0)  # уже 29.10 по Москве
    fx = FxResolver(conn, lambda day: pytest.fail("ЦБ не нужен"))
    assert fx.rate(Currency.RUB, date(2026, 10, 27)) == (Decimal("80.8"), "steam")
    assert fx.rate(Currency.RUB, date(2026, 10, 28)) == (Decimal("81.0"), "steam")
    assert fx.rate(Currency.RUB, date(2026, 11, 5)) == (Decimal("82.0"), "steam")
    assert fx.rate(Currency.USD, date(2020, 1, 1)) == (Decimal(1), "usd")


def test_resolver_without_cbr_currency_raises(conn):
    fx = FxResolver(conn, lambda day: {Currency.RUB: Decimal(70)})
    with pytest.raises(FxUnavailable):
        fx.rate(Currency.UAH, date(2021, 1, 1))
