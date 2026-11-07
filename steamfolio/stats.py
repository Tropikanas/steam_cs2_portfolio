"""Вкладка «Статистика»: сводка, ряды графика, разбивка по типам, лучшие операции. Все суммы в USD."""

from __future__ import annotations

import bisect
import sqlite3
from collections import defaultdict
from itertools import accumulate
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from steamfolio import catalog, views
from steamfolio.clock import MSK, msk_date
from steamfolio.currencies import SYMBOLS, Currency
from steamfolio.ledger import Holding, Ledger, Op, RowKey, daily_holdings, replay


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


def _load_cbr(conn: sqlite3.Connection, currency: Currency) -> _Series:
    result = _Series()
    for day, rate in conn.execute("SELECT date, rate FROM fx_cbr WHERE currency = ? ORDER BY date", (int(currency),)):
        result.add(day, Decimal(rate))
    return result


def series(
    conn: sqlite3.Connection, ops: list[Op], now: datetime | None = None, currency: Currency | None = None,
) -> dict[str, Any]:
    """Линии «Стоимость портфеля» (снимки раз в 6 ч; до них — оценка по истории продаж) и «Вложено».

    currency=None — все строки в USD (Total); иначе только строки этой валюты, суммы в ней же.
    """
    now = now or datetime.now(UTC)
    holdings = daily_holdings(ops)
    if currency is not None:
        holdings = [(d, {k: v for k, v in state.items() if k.currency == currency}) for d, state in holdings]
        while holdings and not holdings[0][1]:
            holdings.pop(0)
    if not holdings:
        return {"value": [], "value_estimate": [], "invested": [], "profit": [], "launch": None,
                "currency": (currency or Currency.USD).name}
    days = [d for d, _ in holdings]

    def held(day: date) -> dict[RowKey, Holding]:
        i = bisect.bisect_right(days, day)
        return holdings[i - 1][1] if i else {}

    items = {k.market_hash_name for _, state in holdings for k in state}
    snapshots = _load_snapshots(conn, items)
    rates = _load_rates(conn)
    history = _load_history(conn, items)
    cbr = _load_cbr(conn, currency) if currency not in (None, Currency.USD) else None
    runs = [r[0] for r in conn.execute(
        """SELECT finished_at FROM collector_runs WHERE kind = 'scheduled' AND status IN ('ok', 'partial')
           AND finished_at IS NOT NULL ORDER BY finished_at"""
    )]
    launch = msk_date(runs[0]) if runs else None

    def cost_of(holding: Holding) -> Decimal:
        return holding[2] if currency is not None else holding[1]

    def price_usd(key: RowKey, ts: str) -> Decimal | None:
        price = snapshots[(key.market_hash_name, int(Currency.USD))].at(ts)
        if price is None and key.currency != Currency.USD:
            local = snapshots[(key.market_hash_name, int(key.currency))].at(ts)
            rate = rates[int(key.currency)].at(ts)
            return Decimal(local) / rate if local is not None and rate else None
        return None if price is None else Decimal(price)

    def price_local(key: RowKey, ts: str) -> Decimal | None:
        """Лучший ордер в валюте строки; если его нет — из USD по курсу Steam."""
        local = snapshots[(key.market_hash_name, int(key.currency))].at(ts)
        if local is not None:
            return Decimal(local)
        usd = snapshots[(key.market_hash_name, int(Currency.USD))].at(ts)
        rate = Decimal(1) if key.currency == Currency.USD else rates[int(key.currency)].at(ts)
        return Decimal(usd) * rate if usd is not None and rate else None

    def history_price(key: RowKey, ts: str) -> Decimal | None:
        """Медиана продаж Steam за день точки — для предмета, который на этот момент ещё не снимался."""
        usd = history[key.market_hash_name].at(msk_date(ts).isoformat())
        if usd is None:
            return None
        if currency is None or currency == Currency.USD:
            return Decimal(usd)
        rate = rates[int(currency)].at(ts) or (cbr.at(msk_date(ts).isoformat()) if cbr else None)
        return Decimal(usd) * rate if rate else None

    def value_at(state: dict[RowKey, Holding], ts: str) -> Decimal:
        total = Decimal(0)
        for key, holding in state.items():
            price = price_usd(key, ts) if currency is None else price_local(key, ts)
            if price is None:
                price = history_price(key, ts)
            # Ни снимка, ни истории продаж — по цене покупки, чтобы предмет не выпал из оценки.
            total += price * holding[0] if price is not None else cost_of(holding)
        return total

    def estimate_at(state: dict[RowKey, Holding], day: date) -> Decimal:
        # История продаж хранится в USD; для валюты строки — по курсу ЦБ на этот день.
        rate = Decimal(1) if cbr is None else cbr.at(day.isoformat()) or (cbr.values[0] if cbr.values else None)
        total = Decimal(0)
        for key, holding in state.items():
            price = history[key.market_hash_name].at(day.isoformat())
            total += Decimal(price) * rate * holding[0] if price is not None and rate else cost_of(holding)
        return total

    def invested(state: dict[RowKey, Holding]) -> Decimal:
        return sum((cost_of(h) for h in state.values()), Decimal(0))

    value, estimate, invested_line = [], [], []
    as_of: list[date] = []  # на конец какого дня взяты остатки каждой точки «Вложено»
    # До первого снимка — ежедневная оценка по истории продаж Steam.
    end = launch or msk_date(now.isoformat())
    day = days[0]
    while day < end:
        state = held(day)
        moment = datetime.combine(day, time(23, 59), MSK)
        estimate.append([_ms(moment), _usd(estimate_at(state, day))])
        invested_line.append([_ms(moment), _usd(invested(state))])
        as_of.append(day)
        day += timedelta(days=1)
    # Последняя точка — «сейчас»: покупки, продажи и удаления видны сразу, не дожидаясь следующего сбора цен.
    moments = list(runs)
    if runs and now > datetime.fromisoformat(runs[-1]):
        moments.append(now.isoformat(timespec="seconds"))
    for i, ts in enumerate(moments):
        # У сделок нет времени, только дата: сделки дня меняют последнюю точку этого дня
        # (сегодня — точку «сейчас»), более ранние точки дня — ещё на конец предыдущего дня.
        day = msk_date(ts)
        last_of_day = i == len(moments) - 1 or msk_date(moments[i + 1]) != day
        state_day = day if last_of_day else day - timedelta(days=1)
        if state_day < days[0]:
            continue  # график начинается с первой покупки, а не с первого сбора цен
        state = held(state_day)
        moment = datetime.fromisoformat(ts)
        value.append([_ms(moment), _usd(value_at(state, ts))])
        invested_line.append([_ms(moment), _usd(invested(state))])
        as_of.append(state_day)
    # Пунктир стыкуется со сплошной линией.
    if estimate and value:
        estimate.append(value[0])
    return {"value": value, "value_estimate": estimate, "invested": invested_line,
            "profit": _realized_line(ops, [(ms, d) for (ms, _), d in zip(invested_line, as_of)], currency),
            "launch": launch.isoformat() if launch else None, "currency": (currency or Currency.USD).name}


def chart_currencies(ledger: Ledger) -> list[dict[str, Any]]:
    """Валюты для переключателя графика: с открытыми позициями или продажами (удалённые позиции не в счёт).

    Порядок — по вложенному сейчас в USD, затем по коду.
    """
    invested: dict[Currency, Decimal] = defaultdict(Decimal)
    for p in ledger.open_positions:
        invested[p.key.currency] += p.cost_usd
    for s in ledger.sales:
        invested.setdefault(s.key.currency, Decimal(0))
    ordered = sorted(invested.items(), key=lambda kv: (-kv[1], kv[0].name))
    return [{"code": c.name, "symbol": SYMBOLS[c], "invested_usd": _usd(v)} for c, v in ordered]


def _realized_line(ops: list[Op], moments: list[tuple[int, date]], currency: Currency | None) -> list[list[float]]:
    """Реализованная прибыль нарастающим итогом на те же моменты, что и «Вложено»: сумма прибыли продаж
    по ту же дату, на конец которой взяты остатки точки."""
    sales = sorted(
        (s.op_date, s.profit_usd if currency is None else s.profit)
        for s in replay(ops).sales if currency is None or s.key.currency == currency
    )
    days = [d for d, _ in sales]
    running = list(accumulate(p for _, p in sales))
    line = []
    for ms, day in moments:
        i = bisect.bisect_right(days, day)
        line.append([ms, _usd(running[i - 1] if i else Decimal(0))])
    return line


def types(conn: sqlite3.Connection, ledger: Ledger, portfolio_id: int = 1) -> dict[str, Any]:
    """Текущая стоимость по категориям, внутри — по подкатегориям (если есть) и предметам
    (без разбивки на аккаунты и валюты). type у предмета — его тип (его можно поменять вручную)."""
    rows = views.portfolio(conn, ledger, portfolio_id=portfolio_id)["rows"]
    rarity = {name: row["rarity"] for name, row in views._items(conn, {r["item"] for r in rows}).items()}
    by_cat: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        value = r.get("value_usd")
        estimated = value is None
        value = r["cost_usd"] if estimated else value
        sub = catalog.subcategory(r["type"], rarity.get(r["item"]))
        item = by_cat[catalog.category(r["type"])].setdefault(r["item"], {
            "item": r["item"], "icon": r["icon"], "url": r["url"], "type": r["type"],
            "subtype": sub[0] if sub else None, "subtype_color": sub[1] if sub else None,
            "subtype_order": sub[2] if sub else 0, "quantity": 0, "value_usd": 0.0, "no_price": False})
        item["quantity"] += r["quantity"]
        item["value_usd"] = round(item["value_usd"] + (value or 0), 2)
        item["no_price"] = item["no_price"] or estimated
    total = sum(i["value_usd"] for items in by_cat.values() for i in items.values())
    def share(part: float, whole: float) -> float:
        return round(part / whole * 100, 2) if whole else 0

    result = []
    for kind, items in by_cat.items():
        value = round(sum(i["value_usd"] for i in items.values()), 2)
        subs: dict[str, dict[str, Any]] = {}
        for i in items.values():
            if i["subtype"] is not None:
                s = subs.setdefault(i["subtype"], {"subtype": i["subtype"], "color": i["subtype_color"],
                                                   "order": i["subtype_order"], "value_usd": 0.0})
                s["value_usd"] = round(s["value_usd"] + i["value_usd"], 2)
        result.append({
            "type": kind,
            "value_usd": value,
            "share": share(value, total),
            "subtypes": sorted(({**s, "share": share(s["value_usd"], value)} for s in subs.values()),
                               key=lambda s: -s["value_usd"]) or None,
            "items": sorted(
                ({**i, "share": share(i["value_usd"], value)} for i in items.values()),
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
            "first_buy_date": s.first_buy_date.isoformat() if s.first_buy_date else None,
            "item": s.key.market_hash_name,
            "currency": s.key.currency.name,
            "quantity": s.quantity,
            "profit": _usd(s.profit),
            "profit_usd": _usd(s.profit_usd),
            "profit_pct": round(float(s.profit_pct), 2),
            "annual_pct": round(float(s.annual_pct), 2),
            "days": s.days_held,
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
