"""Вкладка «Статистика»: сводка, ряды графика, разбивка по типам, лучшие операции. Все суммы в USD."""

from __future__ import annotations

import bisect
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from steamfolio import views
from steamfolio.clock import MSK, msk_date
from steamfolio.currencies import Currency
from steamfolio.ledger import Ledger, Op, RowKey, daily_holdings


def _usd(minor: Decimal | int | None) -> float | None:
    return None if minor is None else round(float(Decimal(minor) / 100), 2)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class _Series:
    """Значения по времени с поиском «последнее известное не позже момента»."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.values: list[Any] = []

    def add(self, key: str, value: Any) -> None:
        self.keys.append(key)
        self.values.append(value)

    def at(self, key: str) -> Any:
        i = bisect.bisect_right(self.keys, key)
        return self.values[i - 1] if i else None


def _load_snapshots(conn: sqlite3.Connection, items: set[str]) -> dict[tuple[str, int], _Series]:
    result: dict[tuple[str, int], _Series] = defaultdict(_Series)
    if not items:
        return result
    marks = ",".join("?" * len(items))
    rows = conn.execute(
        f"""SELECT market_hash_name, currency, taken_at, best_buy FROM price_snapshots
            WHERE market_hash_name IN ({marks}) AND best_buy IS NOT NULL ORDER BY taken_at""",
        tuple(items),
    )
    for name, currency, taken_at, best_buy in rows:
        result[(name, currency)].add(taken_at, best_buy)
    return result


def _load_rates(conn: sqlite3.Connection) -> dict[int, _Series]:
    result: dict[int, _Series] = defaultdict(_Series)
    for currency, taken_at, rate in conn.execute("SELECT currency, taken_at, rate FROM fx_snapshots ORDER BY taken_at"):
        result[currency].add(taken_at, Decimal(str(rate)))
    return result


def _load_history(conn: sqlite3.Connection, items: set[str]) -> dict[str, _Series]:
    result: dict[str, _Series] = defaultdict(_Series)
    if not items:
        return result
    marks = ",".join("?" * len(items))
    for name, day, price in conn.execute(
        f"SELECT market_hash_name, day, price_usd FROM price_history WHERE market_hash_name IN ({marks}) ORDER BY day",
        tuple(items),
    ):
        result[name].add(day, price)
    return result


def series(conn: sqlite3.Connection, ops: list[Op], now: datetime | None = None) -> dict[str, Any]:
    """Линии «Стоимость портфеля» (снимки раз в 6 ч; до них — оценка по истории продаж) и «Вложено»."""
    now = now or datetime.now(UTC)
    holdings = daily_holdings(ops)
    if not holdings:
        return {"value": [], "value_estimate": [], "invested": [], "launch": None}
    days = [d for d, _ in holdings]

    def held(day: date) -> dict[RowKey, tuple[int, Decimal]]:
        i = bisect.bisect_right(days, day)
        return holdings[i - 1][1] if i else {}

    items = {k.market_hash_name for _, state in holdings for k in state}
    snapshots = _load_snapshots(conn, items)
    rates = _load_rates(conn)
    history = _load_history(conn, items)
    runs = [r[0] for r in conn.execute(
        """SELECT finished_at FROM collector_runs WHERE kind = 'scheduled' AND status IN ('ok', 'partial')
           AND finished_at IS NOT NULL ORDER BY finished_at"""
    )]
    launch = msk_date(runs[0]) if runs else None

    def value_at(state: dict[RowKey, tuple[int, Decimal]], ts: str) -> Decimal:
        total = Decimal(0)
        for key, (qty, cost_usd) in state.items():
            price = snapshots[(key.market_hash_name, int(Currency.USD))].at(ts)
            if price is None and key.currency != Currency.USD:
                local = snapshots[(key.market_hash_name, int(key.currency))].at(ts)
                rate = rates[int(key.currency)].at(ts)
                price = Decimal(local) / rate if local is not None and rate else None
            total += Decimal(price) * qty if price is not None else cost_usd
        return total

    def estimate_at(state: dict[RowKey, tuple[int, Decimal]], day: date) -> Decimal:
        total = Decimal(0)
        for key, (qty, cost_usd) in state.items():
            price = history[key.market_hash_name].at(day.isoformat())
            total += Decimal(price) * qty if price is not None else cost_usd
        return total

    def invested(state: dict[RowKey, tuple[int, Decimal]]) -> Decimal:
        return sum((cost for _, cost in state.values()), Decimal(0))

    value, estimate, invested_line = [], [], []
    # До первого снимка — ежедневная оценка по истории продаж Steam.
    end = launch or msk_date(now.isoformat())
    day = days[0]
    while day < end:
        state = held(day)
        moment = datetime.combine(day, time(23, 59), MSK)
        estimate.append([_ms(moment), _usd(estimate_at(state, day))])
        invested_line.append([_ms(moment), _usd(invested(state))])
        day += timedelta(days=1)
    for ts in runs:
        state = held(msk_date(ts))
        moment = datetime.fromisoformat(ts)
        value.append([_ms(moment), _usd(value_at(state, ts))])
        invested_line.append([_ms(moment), _usd(invested(state))])
    # Последняя точка — «сейчас»: покупки, продажи и удаления видны сразу, не дожидаясь следующего сбора цен.
    if runs and now > datetime.fromisoformat(runs[-1]):
        state = held(msk_date(now.isoformat()))
        ts = now.isoformat(timespec="seconds")
        value.append([_ms(now), _usd(value_at(state, ts))])
        invested_line.append([_ms(now), _usd(invested(state))])
    # Пунктир стыкуется со сплошной линией.
    if estimate and value:
        estimate.append(value[0])
    return {"value": value, "value_estimate": estimate, "invested": invested_line,
            "launch": launch.isoformat() if launch else None}


def types(conn: sqlite3.Connection, ledger: Ledger, portfolio_id: int = 1) -> dict[str, Any]:
    """Текущая стоимость по типам, внутри — по предметам (без разбивки на аккаунты и валюты)."""
    rows = views.portfolio(conn, ledger, portfolio_id=portfolio_id)["rows"]
    by_type: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        value = r.get("value_usd")
        estimated = value is None
        value = r["cost_usd"] if estimated else value
        item = by_type[r["type"]].setdefault(r["item"], {"item": r["item"], "icon": r["icon"], "url": r["url"],
                                                         "quantity": 0, "value_usd": 0.0, "no_price": False})
        item["quantity"] += r["quantity"]
        item["value_usd"] = round(item["value_usd"] + (value or 0), 2)
        item["no_price"] = item["no_price"] or estimated
    total = sum(i["value_usd"] for items in by_type.values() for i in items.values())
    result = []
    for kind, items in by_type.items():
        value = round(sum(i["value_usd"] for i in items.values()), 2)
        result.append({
            "type": kind,
            "value_usd": value,
            "share": round(value / total * 100, 2) if total else 0,
            "items": sorted(
                ({**i, "share": round(i["value_usd"] / value * 100, 2) if value else 0} for i in items.values()),
                key=lambda i: -i["value_usd"],
            ),
        })
    result.sort(key=lambda t: -t["value_usd"])
    return {"types": result, "total_usd": round(total, 2)}


def best_operations(ledger: Ledger, ops: list[Op]) -> list[dict[str, Any]]:
    return [
        {
            "op_id": s.op_id,
            "date": s.op_date.isoformat(),
            "item": s.key.market_hash_name,
            "currency": s.key.currency.name,
            "quantity": s.quantity,
            "profit": _usd(s.profit),
            "profit_usd": _usd(s.profit_usd),
            "profit_pct": round(float(s.profit_pct), 2),
            "months": float(s.months),
        }
        for s in ledger.sales
    ]


def summary(conn: sqlite3.Connection, ledger: Ledger, portfolio_id: int = 1) -> dict[str, Any]:
    current = views.portfolio(conn, ledger, portfolio_id=portfolio_id)["summary"]
    return {
        **current,
        "total_invested_usd": _usd(ledger.invested_usd),
        "total_sold_usd": _usd(ledger.sold_usd),
        "total_profit_usd": _usd(ledger.profit_usd),
    }
