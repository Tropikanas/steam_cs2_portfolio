from datetime import UTC, date, datetime, timedelta

import pytest

from steamfolio import catalog, views
from steamfolio.currencies import Currency

RUB, USD = Currency.RUB, Currency.USD
CASE = "Kilowatt Case"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def snapshot(conn, item, currency, best_buy, when, rate=None):
    with conn:
        run = conn.execute("INSERT INTO collector_runs (started_at, finished_at, status) VALUES (?, ?, 'ok')",
                           (when.isoformat(), when.isoformat())).lastrowid
        conn.execute(
            """INSERT INTO price_snapshots (run_id, taken_at, market_hash_name, currency, best_buy, lowest_sell, source)
               VALUES (?, ?, ?, ?, ?, NULL, 'histogram')""",
            (run, when.isoformat(timespec="seconds"), item, int(currency), best_buy),
        )
        if rate:
            conn.execute("INSERT INTO fx_snapshots VALUES (?, ?, 5, ?, 5, 0)", (run, when.isoformat(timespec="seconds"), rate))


def test_classify():
    assert catalog.classify("crates.json", {"type": "Case"}) == catalog.CASES
    assert catalog.classify("crates.json", {"type": "Autograph Capsule"}) == catalog.CAPSULES
    assert catalog.classify("crates.json", {"type": "Souvenir"}) == catalog.OTHER
    assert catalog.classify("stickers.json", {"tournament": {"name": "Austin 2025"}}) == catalog.TOURNAMENT_STICKERS
    assert catalog.classify("stickers.json", {"crates": [{"name": "Operation Riptide Sticker Collection"}]}) == catalog.OPERATION_STICKERS
    assert catalog.classify("stickers.json", {"crates": [{"name": "Community Sticker Capsule 1"}]}) == catalog.OTHER_STICKERS
    assert catalog.classify("skins_not_grouped.json", {"category": {"name": "Gloves"}}) == catalog.KNIVES_GLOVES
    assert catalog.classify("skins_not_grouped.json", {"category": {"name": "Rifles"}}) == catalog.WEAPONS
    assert catalog.classify("agents.json", {}) == catalog.AGENTS
    assert catalog.classify("patches.json", {}) == catalog.PATCHES
    assert catalog.classify("keychains.json", {}) == catalog.OTHER


def test_search_prefers_prefix(conn):
    with conn:
        conn.executemany(
            "INSERT INTO items (market_hash_name, icon_url, auto_type, updated_at) VALUES (?, ?, ?, '')",
            [("Kilowatt Case", "x", catalog.CASES), ("Sticker | Kilowatt", "y", catalog.OTHER_STICKERS),
             ("Fever Case", "z", catalog.CASES)],
        )
    assert [i["name"] for i in catalog.search(conn, "kilo")] == ["Kilowatt Case", "Sticker | Kilowatt"]
    assert [i["name"] for i in catalog.search(conn, "case fev")] == ["Fever Case"]


def test_portfolio_row_prices_and_periods(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 10, 1000, date(2026, 10, 1))  # 10 ₽, курс 80
    snapshot(conn, CASE, RUB, 800, NOW - timedelta(days=30, hours=1))
    snapshot(conn, CASE, RUB, 1000, NOW - timedelta(days=7))
    snapshot(conn, CASE, RUB, 1200, NOW - timedelta(days=1, hours=1))
    snapshot(conn, CASE, RUB, 1500, NOW - timedelta(hours=2))
    snapshot(conn, CASE, USD, 20, NOW - timedelta(hours=2))
    data = views.portfolio(conn, journal.ledger(), now=NOW)
    [row] = data["rows"]
    assert row["quantity"] == 10
    assert row["avg_price"] == 10.0
    assert row["price"] == 15.0
    assert row["pct_avg"] == 50.0
    assert row["pct_1d"] == 25.0
    assert row["pct_7d"] == 50.0
    assert row["pct_30d"] == 87.5
    assert data["summary"] == {"invested_usd": 1.25, "value_usd": 2.0, "profit_usd": 0.75, "unpriced": 0}


def test_period_is_empty_without_old_enough_history(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 1, 1000, date(2026, 10, 1))
    snapshot(conn, CASE, RUB, 1500, NOW - timedelta(hours=2))
    [row] = views.portfolio(conn, journal.ledger(), now=NOW)["rows"]
    assert row["pct_1d"] is None and row["pct_7d"] is None and row["pct_30d"] is None


def test_collapse_accounts_weighted_average(conn, journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 1, 1000, date(2026, 10, 1))
    journal.buy(CASE, acc2, RUB, 3, 2000, date(2026, 10, 5))
    journal.buy(CASE, acc2, USD, 1, 50, date(2026, 10, 5))
    data = views.portfolio(conn, journal.ledger(), collapse=True, now=NOW)
    rows = {(r["currency"]): r for r in data["rows"]}
    assert len(data["rows"]) == 2  # валюты не схлопываются
    assert rows["RUB"]["quantity"] == 4
    assert rows["RUB"]["avg_price"] == 17.5
    assert rows["RUB"]["first_buy_date"] == "2026-10-01"
    assert {a["quantity"] for a in rows["RUB"]["accounts"]} == {1, 3}


def test_unpriced_item_counted_at_cost(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 2, 1000, date(2026, 10, 1))
    summary = views.portfolio(conn, journal.ledger(), now=NOW)["summary"]
    assert summary == {"invested_usd": 0.25, "value_usd": 0.25, "profit_usd": 0.0, "unpriced": 1}


def test_closed_positions_are_sales(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 4, 1000, date(2026, 9, 1))  # 10 ₽
    journal.buy(CASE, acc, RUB, 4, 2000, date(2026, 9, 11))  # средняя 15 ₽
    journal.sell(CASE, acc, RUB, 3, 3000, date(2026, 10, 1))  # по 30 ₽: +100 %, 30 дней с 01.09
    journal.sell(CASE, acc, RUB, 5, 1500, date(2026, 10, 1))  # по 15 ₽: 0 %
    journal.buy(CASE, acc, RUB, 1, 1000, date(2026, 10, 2))
    journal.sell(CASE, acc, RUB, 1, 1100, date(2026, 10, 2))  # в тот же день: +10 % за 1 день
    assert views.portfolio(conn, journal.ledger(), now=NOW)["rows"] == []
    rows = views.portfolio(conn, journal.ledger(), closed=True, now=NOW)["rows"]
    got = [(r["quantity"], r["avg_price"], r["sale_price"], r["first_buy_date"], r["sale_date"], r["days"],
            r["profit_pct"], r["annual_pct"]) for r in rows]
    assert got == [
        (1, 10.0, 11.0, "2026-10-02", "2026-10-02", 1, 10.0, 3650.0),
        (5, 15.0, 15.0, "2026-09-01", "2026-10-01", 30, 0.0, 0.0),
        (3, 15.0, 30.0, "2026-09-01", "2026-10-01", 30, 100.0, 1216.67),
    ]


def test_orders_view_outbid_and_reserved(conn, journal, acc):
    snapshot(conn, CASE, RUB, 1100, NOW - timedelta(hours=1), rate=80.0)
    journal.place_order(CASE, acc, RUB, 10, 1000)
    data = views.orders(conn, journal.ledger())
    [row] = data["rows"]
    assert row["outbid"] is True
    assert row["total"] == 100.0
    assert data["summary"]["reserved_usd"] == pytest.approx(1.25)
