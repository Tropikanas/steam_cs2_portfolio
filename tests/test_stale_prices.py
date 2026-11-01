from datetime import UTC, date, datetime, timedelta

from steamfolio import collector, views
from steamfolio.currencies import Currency

RUB, USD = Currency.RUB, Currency.USD
CASE, AK = "Kilowatt Case", "AK-47 | Redline (Field-Tested)"


def snapshot(conn, item, currency, taken_at):
    with conn:
        run = conn.execute(
            "INSERT INTO collector_runs (started_at, finished_at, status) VALUES (?, ?, 'ok')", (taken_at, taken_at)
        ).lastrowid
        conn.execute(
            """INSERT INTO price_snapshots (run_id, taken_at, market_hash_name, currency, best_buy, lowest_sell, source)
               VALUES (?, ?, ?, ?, 100, NULL, 'histogram')""",
            (run, taken_at, item, int(currency)),
        )


def iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat(timespec="seconds")


def test_stale_targets_find_old_and_missing_prices(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 1, 100, date(2026, 10, 1))
    journal.buy(AK, acc, RUB, 1, 100, date(2026, 10, 1))
    snapshot(conn, CASE, RUB, iso(timedelta(hours=1)))
    snapshot(conn, CASE, USD, iso(timedelta(hours=1)))
    snapshot(conn, AK, RUB, iso(timedelta(days=4)))  # позиция вернулась после удаления продажи — цена старая
    assert collector.stale_targets(conn) == [collector.Target(AK, (RUB, USD))]  # USD у AK не было вовсе


def test_portfolio_marks_stale_price(conn, journal, acc):
    journal.buy(CASE, acc, RUB, 1, 100, date(2026, 10, 1))
    snapshot(conn, CASE, RUB, iso(timedelta(days=4)))
    [row] = views.portfolio(conn, journal.ledger())["rows"]
    assert row["price_stale"] is True
    snapshot(conn, CASE, RUB, iso(timedelta(minutes=5)))
    [row] = views.portfolio(conn, journal.ledger())["rows"]
    assert row["price_stale"] is False
