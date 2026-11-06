"""Данные для вкладок «Портфолио» и «Ордера»: записи покупок и продаж + история цен + справочники.

Деньги отдаются числами в единицах валюты (рубли, доллары), проценты — числами в процентах.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from steamfolio import catalog
from steamfolio.collector import STALE_AFTER, latest_rates
from steamfolio.currencies import NAMES, SYMBOLS, USER_CURRENCIES, Currency
from steamfolio.journal import Order
from steamfolio.ledger import Ledger, Op, Position
from steamfolio.market import listing_url

# Снимок «24 ч назад» может быть сделан чуть позже: сбор идёт раз в 6 часов со случайной задержкой.
SNAPSHOT_TOLERANCE = timedelta(hours=3)
PERIODS = {"pct_1d": timedelta(days=1), "pct_7d": timedelta(days=7), "pct_30d": timedelta(days=30)}


def _units(minor: Decimal | int | None, digits: int = 2) -> float | None:
    return None if minor is None else round(float(Decimal(minor) / 100), digits)


def _pct(now: int | None, then: Decimal | int | None) -> float | None:
    if now is None or not then:
        return None
    return round(float((Decimal(now) / Decimal(then) - 1) * 100), 2)


@dataclass
class Prices:
    """Лучший ордер из истории: текущий и в прошлые моменты."""

    conn: sqlite3.Connection
    now: datetime

    def latest(self, item: str, currency: Currency) -> tuple[int | None, str | None]:
        row = self.conn.execute(
            """SELECT best_buy, taken_at FROM price_snapshots WHERE market_hash_name = ? AND currency = ?
               ORDER BY taken_at DESC LIMIT 1""",
            (item, int(currency)),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def at(self, item: str, currency: Currency, moment: datetime) -> int | None:
        row = self.conn.execute(
            """SELECT best_buy FROM price_snapshots WHERE market_hash_name = ? AND currency = ? AND taken_at <= ?
               ORDER BY taken_at DESC LIMIT 1""",
            (item, int(currency), (moment + SNAPSHOT_TOLERANCE).isoformat(timespec="seconds")),
        ).fetchone()
        return row[0] if row else None


def _items(conn: sqlite3.Connection, names: set[str]) -> dict[str, sqlite3.Row]:
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    rows = conn.execute(f"SELECT * FROM items WHERE market_hash_name IN ({marks})", tuple(names)).fetchall()
    return {r["market_hash_name"]: r for r in rows}


def accounts(conn: sqlite3.Connection, portfolio_id: int = 1) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT a.id, a.name, a.hidden,
                  EXISTS (SELECT 1 FROM operations o WHERE o.account_id = a.id OR o.to_account_id = a.id) AS used
           FROM accounts a WHERE a.portfolio_id = ? ORDER BY a.name""",
        (portfolio_id,),
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "hidden": bool(r["hidden"]), "used": bool(r["used"])} for r in rows]


def last_update(conn: sqlite3.Connection) -> str | None:
    return conn.execute(
        "SELECT MAX(finished_at) FROM collector_runs WHERE kind = 'scheduled' AND status IN ('ok', 'partial')"
    ).fetchone()[0]


def _joined(comments: list[str]) -> str:
    """Комментарии нескольких строк через «; », без повторов."""
    parts: list[str] = []
    for c in comments:
        for p in (c or "").split(";"):
            if p.strip() and p.strip() not in parts:
                parts.append(p.strip())
    return "; ".join(parts)


def _buy_info(op: Op, account_names: dict[int, str]) -> dict[str, Any]:
    return {"op_id": op.id, "kind": op.kind, "date": op.op_date.isoformat(), "quantity": op.quantity,
            "price": _units(op.price), "currency": op.currency.name, "account_id": op.account_id,
            "account": account_names.get(op.account_id, "?")}


def portfolio(
    conn: sqlite3.Connection, ledger: Ledger, *, collapse: bool = False, closed: bool = False,
    portfolio_id: int = 1, now: datetime | None = None, ops: list[Op] | None = None,
) -> dict[str, Any]:
    """Строка на каждую покупку (с остатком); collapse — итоги по предмету и валюте, только для просмотра.
    ops — записи: с ними у строки есть её покупка для правки в ячейке (single_op) и комментарий."""
    if closed:
        return sales(conn, ledger, portfolio_id, ops)
    lots = {op.id: op for op in ops or [] if op.kind == "buy"}
    now = now or datetime.now(UTC)
    prices = Prices(conn, now)
    rates = latest_rates(conn)
    account_names = {a["id"]: a["name"] for a in accounts(conn, portfolio_id)}
    positions = ledger.open_positions
    if collapse:
        groups: dict[tuple, list[Position]] = {}
        for p in positions:
            groups.setdefault((p.key.market_hash_name, p.key.currency), []).append(p)
    else:
        groups = {p.key: [p] for p in positions}
    items = _items(conn, {p.key.market_hash_name for p in positions})

    rows = []
    invested = value = Decimal(0)
    unpriced = 0
    for group in groups.values():
        first = group[0]
        name, currency = first.key.market_hash_name, first.key.currency
        quantity = sum(p.quantity for p in group)
        cost = sum((p.cost for p in group), Decimal(0))
        cost_usd = sum((p.cost_usd for p in group), Decimal(0))
        avg = cost / quantity if quantity else None
        price, price_at = prices.latest(name, currency)
        row: dict[str, Any] = {
            "item": name,
            "icon": (items.get(name) or {"icon_url": None})["icon_url"],
            "type": catalog.item_type(items.get(name)),
            "url": listing_url(name),
            "quantity": quantity,
            "avg_price": _units(avg, 4),
            "avg_price_usd": _units(cost_usd / quantity, 4) if quantity else None,
            "first_buy_date": min(p.first_buy_date for p in group).isoformat(),
            "currency": currency.name,
            "price": _units(price),
            "price_at": price_at,
            # Цена давно не обновлялась (сбор её пропускал) — на сайте помечается.
            "price_stale": price_at is not None and datetime.fromisoformat(price_at) < now - STALE_AFTER,
            "pct_avg": _pct(price, avg),
            "accounts": [{"id": p.key.account_id, "name": account_names.get(p.key.account_id, "?"),
                          "quantity": p.quantity, "cost": _units(p.cost)} for p in group],
            "account": ", ".join(dict.fromkeys(account_names.get(p.key.account_id, "?") for p in group)),
            "account_id": first.key.account_id if not collapse else None,
            "comment": _joined([lots[p.key.lot].comment for p in group if p.key.lot in lots]),
            "cost_usd": _units(cost_usd),
            "lot_id": first.key.lot if not collapse else None,
            "position_no": lots[first.key.lot].position_no if not collapse and first.key.lot in lots else None,
            "lots": len(group),
        }
        for column, delta in PERIODS.items():
            row[column] = _pct(price, prices.at(name, currency, now - delta))
        # Строка — одна покупка, она правится прямо в ячейке. bought — сколько куплено ею (остальное продано
        # или перенесено): количество в ячейке — остаток, а в покупке — bought.
        lot = lots.get(first.key.lot) if not collapse else None
        row["single_op"] = _buy_info(lot, account_names) if lot else None
        row["bought"] = first.bought if not collapse else None
        if quantity:
            invested += cost_usd
            price_usd, _ = prices.latest(name, Currency.USD)
            if price_usd is None and price is not None and (currency == Currency.USD or currency in rates):
                price_usd = Decimal(price) / Decimal(str(rates.get(currency, 1)))
            if price_usd is None:
                unpriced += 1
                value += cost_usd  # без цены считаем по себестоимости, чтобы не исказить прибыль
            else:
                value += Decimal(price_usd) * quantity
            row["value_usd"] = _units(Decimal(price_usd) * quantity) if price_usd is not None else None
        rows.append(row)

    rows.sort(key=lambda r: (r["item"], r["currency"], r["account"], r["first_buy_date"], r["lot_id"] or 0))
    return {
        "rows": rows,
        "summary": {
            "invested_usd": _units(invested),
            "value_usd": _units(value),
            "profit_usd": _units(value - invested),
            "unpriced": unpriced,
        },
        "last_update": last_update(conn),
    }


def steam_rates(conn: sqlite3.Connection) -> dict[str, Any]:
    """Курсы Steam последнего сбора и лоты, из которых они посчитаны, — чтобы их можно было проверить на площадке."""
    run = conn.execute(
        "SELECT run_id, MAX(taken_at) FROM fx_snapshots GROUP BY run_id ORDER BY MAX(taken_at) DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return {"taken_at": None, "rates": [], "items": []}
    run_id, taken_at = run
    rates = []
    for r in conn.execute("SELECT * FROM fx_snapshots WHERE run_id = ? ORDER BY currency", (run_id,)):
        currency = Currency(r["currency"])
        # Курс сутки назад — для сравнения (ближайший снимок не позже).
        day_ago = conn.execute(
            """SELECT rate FROM fx_snapshots WHERE currency = ? AND taken_at <= ? ORDER BY taken_at DESC LIMIT 1""",
            (r["currency"], (datetime.fromisoformat(r["taken_at"]) - timedelta(days=1) + SNAPSHOT_TOLERANCE).isoformat(timespec="seconds")),
        ).fetchone()
        rates.append({
            "code": currency.name, "symbol": SYMBOLS[currency], "name": NAMES[currency],
            "rate": round(r["rate"], 4), "usd_per_unit": round(1 / r["rate"], 6) if r["rate"] else None,
            "samples": r["samples"], "spread_pct": round(r["spread_pct"], 2),
            "pct_1d": round((r["rate"] / day_ago[0] - 1) * 100, 2) if day_ago and day_ago[0] else None,
        })
    rates.sort(key=lambda x: USER_CURRENCIES.index(Currency[x["code"]]))
    items: dict[str, dict[str, Any]] = {}
    for s in conn.execute("SELECT * FROM fx_samples WHERE run_id = ? ORDER BY rowid", (run_id,)):
        item = items.setdefault(s["market_hash_name"], {"item": s["market_hash_name"], "url": listing_url(s["market_hash_name"]),
                                                          "prices": {}})
        item["prices"][Currency(s["currency"]).name] = _units(s["lowest_sell"])
    return {"taken_at": taken_at, "rates": rates, "items": list(items.values())}


def sales(conn: sqlite3.Connection, ledger: Ledger, portfolio_id: int = 1, ops: list[Op] | None = None) -> dict[str, Any]:
    """«Закрытые позиции»: строка на каждую продажу — что продано, по какой цене покупки, с какой доходностью.
    Комментарий — свой у продажи (вводится при «Продано»), из строки портфеля не копируется."""
    account_names = {a["id"]: a["name"] for a in accounts(conn, portfolio_id)}
    by_id = {op.id: op for op in ops or []}
    items = _items(conn, {s.key.market_hash_name for s in ledger.sales})
    rows = []
    for s in ledger.sales:
        name, account_id, currency, lot = s.key
        sale, bought = by_id.get(s.op_id), by_id.get(lot)
        comment = sale.comment if sale else ""
        rows.append({
            "op_id": s.op_id,
            "item": name,
            "icon": (items.get(name) or {"icon_url": None})["icon_url"],
            "type": catalog.item_type(items.get(name)),
            "url": listing_url(name),
            "quantity": s.quantity,
            "avg_price": _units(s.avg_price, 4),
            "avg_price_usd": _units(s.avg_price_usd, 4),
            "sale_price": _units(s.price),
            "first_buy_date": s.first_buy_date.isoformat() if s.first_buy_date else None,
            "sale_date": s.op_date.isoformat(),
            "days": s.days_held,
            "currency": currency.name,
            "profit": _units(s.profit),
            "profit_usd": _units(s.profit_usd),
            "profit_pct": round(float(s.profit_pct), 2),
            "annual_pct": round(float(s.annual_pct), 2),
            "account": account_names.get(account_id, "?"),
            "account_id": account_id,
            "lot_id": lot,
            "position_no": bought.position_no if bought else None,
            "comment": comment,
        })
    rows.sort(key=lambda r: (r["sale_date"], r["op_id"]), reverse=True)
    return {"rows": rows, "summary": None, "last_update": last_update(conn)}


def orders(conn: sqlite3.Connection, open_orders: list[Order], portfolio_id: int = 1) -> dict[str, Any]:
    prices = Prices(conn, datetime.now(UTC))
    rates = latest_rates(conn)
    account_names = {a["id"]: a["name"] for a in accounts(conn, portfolio_id)}
    items = _items(conn, {o.market_hash_name for o in open_orders})
    rows = []
    reserved = Decimal(0)
    for o in open_orders:
        market, market_at = prices.latest(o.market_hash_name, o.currency)
        rate = Decimal(1) if o.currency == Currency.USD else Decimal(str(rates[o.currency])) if o.currency in rates else None
        if rate:
            reserved += Decimal(o.total) / rate
        rows.append({
            "order_id": o.order_id,
            "item": o.market_hash_name,
            "icon": (items.get(o.market_hash_name) or {"icon_url": None})["icon_url"],
            "url": listing_url(o.market_hash_name),
            "quantity": o.quantity,
            "price": _units(o.price),
            "currency": o.currency.name,
            "account_id": o.account_id,
            "account": account_names.get(o.account_id, "?"),
            "market_best": _units(market),
            "market_at": market_at,
            "outbid": market is not None and market > o.price,
            "total": _units(o.total),
            "comment": o.comment,
        })
    rows.sort(key=lambda r: (r["item"], r["currency"], r["account"]))
    return {"rows": rows, "summary": {"reserved_usd": _units(reserved)}, "last_update": last_update(conn)}
