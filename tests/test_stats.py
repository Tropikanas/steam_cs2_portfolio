from datetime import UTC, date, datetime
from decimal import Decimal

from steamfolio import catalog, stats
from steamfolio.currencies import Currency
from steamfolio.fx import parse_cbr_dynamic
from steamfolio.ledger import replay

RUB, USD = Currency.RUB, Currency.USD
CASE, AK = "Kilowatt Case", "AK-47 | Redline (Field-Tested)"
D = date.fromisoformat


def run_with_prices(conn, finished_at, prices):
    """Плановый запуск сборщика с ценами {(предмет, валюта): лучший ордер}."""
    with conn:
        run = conn.execute(
            "INSERT INTO collector_runs (started_at, finished_at, status) VALUES (?, ?, 'ok')", (finished_at, finished_at)
        ).lastrowid
        for (item, cur), price in prices.items():
            conn.execute(
                """INSERT INTO price_snapshots (run_id, taken_at, market_hash_name, currency, best_buy, lowest_sell, source)
                   VALUES (?, ?, ?, ?, ?, NULL, 'histogram')""",
                (run, finished_at, item, int(cur), price),
            )


def points(series):
    return [(datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="minutes"), y) for ms, y in series]


def test_series_value_and_invested(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 10, 800, D("2026-09-28"))  # $1.00 по курсу 80
    run_with_prices(conn, "2026-09-28T06:00:00+00:00", {(CASE, USD): 12})
    journal.buy(CASE, acc, RUB, 10, 1600, D("2026-09-29"))  # ещё $2.00
    run_with_prices(conn, "2026-09-29T06:00:00+00:00", {(CASE, USD): 15})
    journal.sell(CASE, acc, RUB, 5, 2000, D("2026-09-30"))  # себестоимость проданного $0.75
    run_with_prices(conn, "2026-09-30T06:00:00+00:00", {(CASE, USD): 20})

    s = stats.series(conn, journal.ops(), now=datetime(2026, 9, 30, 12, tzinfo=UTC))
    assert s["launch"] == "2026-09-28"
    assert s["value_estimate"] == []
    assert [y for _, y in s["value"]] == [1.2, 3.0, 3.0]  # 10×12¢, 20×15¢, 15×20¢
    assert [y for _, y in s["invested"]] == [1.0, 3.0, 2.25]


def test_series_estimate_before_launch_uses_history(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 10, 800, D("2021-11-15"))
    with conn:
        conn.executemany("INSERT INTO price_history VALUES (?, ?, ?, 1)",
                         [(CASE, "2021-11-15", 30), (CASE, "2021-11-17", 50)])
    run_with_prices(conn, "2021-11-18T06:00:00+00:00", {(CASE, USD): 40})
    s = stats.series(conn, journal.ops(), now=datetime(2021, 11, 18, 12, tzinfo=UTC))
    # 15.11 — 30¢, 16.11 — последнее известное (30¢), 17.11 — 50¢; затем стык со снимком
    assert [y for _, y in s["value_estimate"]] == [3.0, 3.0, 5.0, 4.0]
    assert s["value_estimate"][-1] == s["value"][0]


def test_series_falls_back_to_row_currency_and_cost(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 1, 800, D("2026-09-28"))
    journal.buy(AK, acc, RUB, 1, 8000, D("2026-09-28"))
    run_with_prices(conn, "2026-09-28T06:00:00+00:00", {(CASE, RUB): 1600})
    with conn:
        conn.execute("INSERT INTO fx_snapshots VALUES (1, '2026-09-28T05:00:00+00:00', 5, 80.0, 5, 0)")
    s = stats.series(conn, journal.ops(), now=datetime(2026, 9, 28, 12, tzinfo=UTC))
    # кейс: 16 ₽ / 80 = $0.20; AK без цены — по себестоимости $1.00
    assert s["value"][0][1] == 1.2


def test_types_and_drilldown(conn, journal, acc):
    with conn:
        conn.executemany(
            "INSERT INTO items (market_hash_name, icon_url, auto_type, updated_at) VALUES (?, NULL, ?, '')",
            [(CASE, catalog.CASES), (AK, catalog.WEAPONS)],
        )
    journal.buy(CASE, acc, RUB, 10, 800, D("2026-09-28"))
    journal.buy(AK, acc, USD, 1, 300, D("2026-09-28"))
    run_with_prices(conn, "2026-09-28T06:00:00+00:00", {(CASE, USD): 10, (AK, USD): 300})
    result = stats.types(conn, replay(journal.ops()))
    assert result["total_usd"] == 4.0
    assert [(t["type"], t["value_usd"], t["share"]) for t in result["types"]] == [
        (catalog.WEAPONS, 3.0, 75.0), (catalog.CASES, 1.0, 25.0)]
    catalog.set_type(conn, AK, catalog.OTHER)
    assert stats.types(conn, replay(journal.ops()))["types"][0]["type"] == catalog.OTHER


def test_summary_and_best_operations(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 1, 100000, D("2026-01-10"))
    journal.sell(CASE, acc, RUB, 1, 200000, D("2026-04-10"))
    journal.buy(AK, acc, USD, 2, 1000, D("2026-01-10"))
    journal.sell(AK, acc, USD, 2, 1100, D("2026-02-10"))
    ledger = replay(journal.ops())
    s = stats.summary(conn, ledger)
    assert (s["total_invested_usd"], s["total_sold_usd"], s["total_profit_usd"]) == (32.5, 47.0, 14.5)
    best = {b["item"]: b for b in stats.best_operations(ledger, journal.ops())}
    assert best[CASE]["profit_pct"] == 100.0 and best[CASE]["profit_usd"] == 12.5 and best[CASE]["months"] == 3.0
    assert best[AK]["profit_pct"] == 10.0 and best[AK]["profit_usd"] == 2.0


def test_history_daily_usd_from_eur():
    from steamfolio.history import daily_usd

    def rub_rates(currency, start, end):  # ЦБ: $1 = 70 ₽, €1 = 80 ₽ (только 15.11, дальше — тот же курс)
        return {D("2021-11-15"): Decimal(70) if currency == USD else Decimal(80)}

    ts = lambda iso: int(datetime.fromisoformat(iso).timestamp())  # noqa: E731
    points = [
        (ts("2021-11-15T00:00:00+00:00"), 1.0, 10),
        (ts("2021-11-16T10:00:00+00:00"), 2.0, 1),  # почасовые точки — средневзвешенно по продажам
        (ts("2021-11-16T11:00:00+00:00"), 3.0, 3),
        (ts("2021-11-17T00:00:00+00:00"), 9.0, 1),  # уже день начала снимков — не нужен
    ]
    result = daily_usd(Currency.EUR, points, D("2021-11-17"), rub_rates)
    # €1 = 80/70 $ ⇒ 1 € → 114 ¢; 16.11: (2×1 + 3×3)/4 = 2.75 € → 314 ¢
    assert result == {D("2021-11-15"): 114, D("2021-11-16"): 314}


def test_items_needing_history(conn, journal, acc):
    from steamfolio.history import items_needing_history

    journal.buy(CASE, acc, RUB, 1, 100, D("2021-11-15"))
    journal.buy(AK, acc, RUB, 1, 100, D("2026-09-28"))
    assert items_needing_history(conn, journal.ops(), D("2026-09-27")) == [CASE]
    with conn:
        conn.execute("INSERT INTO price_history_fetched VALUES (?, '', 5, NULL)", (CASE,))
    assert items_needing_history(conn, journal.ops(), D("2026-09-27")) == []


def test_parse_cbr_dynamic():
    xml = """<?xml version="1.0" encoding="windows-1251"?><ValCurs ID="R01239">
      <Record Date="13.11.2021" Id="R01239"><Nominal>1</Nominal><Value>82,3876</Value></Record>
      <Record Date="16.11.2021" Id="R01239"><Nominal>1</Nominal><Value>82,0000</Value></Record></ValCurs>""".encode("cp1251")
    assert parse_cbr_dynamic(xml) == {D("2021-11-13"): Decimal("82.3876"), D("2021-11-16"): Decimal("82.0000")}
