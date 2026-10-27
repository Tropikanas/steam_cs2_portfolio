"""Оценка стоимости портфеля до начала собственных снимков: история продаж Steam в USD.

Для каждого предмета, купленного раньше первого планового сбора, один раз загружается
история медианных цен продаж. Цены пересчитываются в центы USD по курсу ЦБ на каждый день
(Steam отдаёт их в валюте страны сервера) и сохраняются только за дни до начала снимков.
"""

from __future__ import annotations

import bisect
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from steamfolio.clock import MSK, msk_date, today_msk
from steamfolio.currencies import Currency
from steamfolio.db import utcnow
from steamfolio.fx import cbr_rub_per_unit
from steamfolio.ledger import Op, effective_ops
from steamfolio.market import MarketError, SteamHttp, fetch_price_history

log = logging.getLogger(__name__)

RubRates = Callable[[Currency, date, date], dict[date, Decimal]]


def launch_date(conn: sqlite3.Connection) -> date:
    """День первого планового сбора: с него стоимость считается по собственным снимкам."""
    first = conn.execute(
        "SELECT MIN(finished_at) FROM collector_runs WHERE kind = 'scheduled' AND status IN ('ok', 'partial')"
    ).fetchone()[0]
    return msk_date(first) if first else today_msk()


def items_needing_history(conn: sqlite3.Connection, ops: list[Op], launch: date) -> list[str]:
    early = sorted({
        op.market_hash_name for op in effective_ops(ops)
        if op.kind in ("buy", "order_filled") and op.op_date < launch
    })
    done = {r[0] for r in conn.execute("SELECT market_hash_name FROM price_history_fetched WHERE error IS NULL")}
    return [name for name in early if name not in done]


def daily_usd(
    currency: Currency, points: list[tuple[int, float, int]], launch: date, rub_rates: RubRates,
) -> dict[date, int]:
    """Дневная цена в центах USD: средняя по продажам за московские сутки, только до launch."""
    by_day: dict[date, list[tuple[float, int]]] = defaultdict(list)
    for ts, price, volume in points:
        day = datetime.fromtimestamp(ts, UTC).astimezone(MSK).date()
        if day < launch:
            by_day[day].append((price, volume))
    if not by_day:
        return {}
    start, end = min(by_day), max(by_day)
    if currency == Currency.USD:
        per_usd = lambda day: Decimal(1)  # noqa: E731
    else:
        usd = _carry_forward(rub_rates(Currency.USD, start, end))
        local = _carry_forward(rub_rates(currency, start, end)) if currency != Currency.RUB else None

        def per_usd(day: date) -> Decimal | None:
            usd_rub = usd(day)
            if usd_rub is None:
                return None
            if local is None:
                return usd_rub
            local_rub = local(day)
            return usd_rub / local_rub if local_rub else None

    result = {}
    for day, values in by_day.items():
        volume = sum(v for _, v in values)
        price = sum(p * v for p, v in values) / volume if volume else sum(p for p, _ in values) / len(values)
        rate = per_usd(day)  # единиц валюты Steam за 1 USD
        if rate:
            result[day] = round(Decimal(str(price)) / rate * 100)
    return result


def _carry_forward(rates: dict[date, Decimal]) -> Callable[[date], Decimal | None]:
    """В выходные ЦБ курс не устанавливает — берём последний известный."""
    days = sorted(rates)

    def at(day: date) -> Decimal | None:
        i = bisect.bisect_right(days, day)
        return rates[days[i - 1]] if i else (rates[days[0]] if days else None)

    return at


def backfill(
    conn: sqlite3.Connection, http: SteamHttp, ops: list[Op], rub_rates: RubRates = cbr_rub_per_unit,
    limit: int = 30,
) -> int:
    """Загружает историю для предметов, купленных до начала снимков. Не больше limit предметов за раз."""
    launch = launch_date(conn)
    loaded = 0
    for name in items_needing_history(conn, ops, launch)[:limit]:
        try:
            currency, points = fetch_price_history(http, name)
            prices = daily_usd(currency, points, launch, rub_rates)
            error = None
        except (MarketError, OSError, ValueError) as e:
            log.warning("История цен %r не загружена: %s", name, e)
            prices, error = {}, str(e)[:300]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO price_history (market_hash_name, day, price_usd, volume) VALUES (?, ?, ?, NULL)",
                [(name, day.isoformat(), price) for day, price in prices.items()],
            )
            conn.execute(
                """INSERT OR REPLACE INTO price_history_fetched (market_hash_name, fetched_at, points, error)
                   VALUES (?, ?, ?, ?)""",
                (name, utcnow(), len(prices), error),
            )
        loaded += error is None
    if loaded:
        log.info("История цен загружена для %d предметов", loaded)
    return loaded
