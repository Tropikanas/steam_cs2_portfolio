"""Данные для вкладок «Портфолио» и «Ордера»: журнал + история цен + справочники.

Деньги отдаются числами в единицах валюты (рубли, доллары), проценты — числами в процентах.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from steamfolio import catalog
from steamfolio.collector import latest_rates
from steamfolio.currencies import Currency
from steamfolio.journal import KIND_NAMES, Preview
from steamfolio.ledger import DELETE_KINDS, EDIT_KINDS, Ledger, Op, Position, effective_ops
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


def portfolio(
    conn: sqlite3.Connection, ledger: Ledger, *, collapse: bool = False, closed: bool = False,
    portfolio_id: int = 1, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    prices = Prices(conn, now)
    rates = latest_rates(conn)
    account_names = {a["id"]: a["name"] for a in accounts(conn, portfolio_id)}
    comments = {
        (r["market_hash_name"], r["account_id"], r["currency"]): r["comment"]
        for r in conn.execute("SELECT * FROM position_comments WHERE portfolio_id = ?", (portfolio_id,))
    }
    positions = ledger.closed_positions if closed else ledger.open_positions
    if collapse:
        groups: dict[tuple[str, Currency], list[Position]] = {}
        for p in positions:
            groups.setdefault((p.key.market_hash_name, p.key.currency), []).append(p)
    else:
        groups = {(p.key.market_hash_name, p.key.currency, p.key.account_id): [p] for p in positions}
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
            "pct_avg": _pct(price, avg),
            "accounts": [{"id": p.key.account_id, "name": account_names.get(p.key.account_id, "?"),
                          "quantity": p.quantity, "cost": _units(p.cost)} for p in group],
            "account": ", ".join(account_names.get(p.key.account_id, "?") for p in group),
            "account_id": first.key.account_id if not collapse else None,
            "comment": comments.get((name, first.key.account_id, int(currency)), "") if not collapse else "",
            "cost_usd": _units(cost_usd),
        }
        for column, delta in PERIODS.items():
            row[column] = _pct(price, prices.at(name, currency, now - delta))
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

    rows.sort(key=lambda r: (r["item"], r["currency"], r["account"]))
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


FIELD_NAMES = {
    "op_date": "дата", "market_hash_name": "предмет", "account_id": "аккаунт", "currency": "валюта",
    "quantity": "количество", "price": "цена", "fx_rate": "курс", "comment": "комментарий",
}
TRADE_KINDS = ("buy", "sell", "order_filled")


def _op_values(op: Op, account_names: dict[int, str]) -> dict[str, Any]:
    """Поля операции в виде для формы изменения."""
    return {
        "date": op.op_date.isoformat(),
        "item": op.market_hash_name,
        "account_id": op.account_id,
        "account": account_names.get(op.account_id, "?"),
        "currency": op.currency.name if op.currency is not None else None,
        "quantity": op.quantity,
        "price": _units(op.price),
        "fx_rate": float(op.fx_rate) if op.fx_rate is not None else None,
        "fx_source": op.fx_source,
        "comment": op.comment,
    }


def _field_text(field: str, value: Any, currency: Any, account_names: dict[int, str]) -> str:
    if value is None or value == "":
        return "—"
    if field == "op_date":
        return datetime.fromisoformat(value).strftime("%d.%m.%Y")
    if field == "account_id":
        return account_names.get(value, "?")
    if field == "currency":
        return Currency(value).name
    if field == "price":
        cur = Currency(currency).name if currency is not None else ""
        return f"{Decimal(value) / 100:,.2f}".replace(",", " ").replace(".", ",") + f" {cur}".rstrip()
    if field == "fx_rate":
        return f"{Decimal(value):.4f}".replace(".", ",")
    return str(value)


def journal(conn: sqlite3.Connection, ops: list[Op], portfolio_id: int = 1) -> dict[str, Any]:
    account_names = {a["id"]: a["name"] for a in accounts(conn, portfolio_id)}
    by_id = {op.id: op for op in ops}
    current = {op.id: op for op in effective_ops(ops)}
    edited_by: dict[int, list[int]] = {}
    deleted_by: dict[int, list[int]] = {}
    for op in ops:
        if op.kind in EDIT_KINDS:
            edited_by.setdefault(op.target_id, []).append(op.id)
        elif op.kind in DELETE_KINDS:
            deleted_by.setdefault(op.target_id, []).append(op.id)
    history: dict[int, list[dict[str, str]]] = {}
    for row in conn.execute(
        """SELECT c.operation_id, c.action, c.at FROM operation_cancellations c
           JOIN operations o ON o.id = c.operation_id WHERE o.portfolio_id = ? ORDER BY c.id""",
        (portfolio_id,),
    ):
        history.setdefault(row["operation_id"], []).append({"action": row["action"], "at": row["at"]})
    items = _items(conn, {op.market_hash_name for op in ops if op.market_hash_name})

    rows = []
    for op in ops:
        changes = []
        if op.kind in EDIT_KINDS:
            before, after = op.payload["before"], op.payload["after"]
            for field, label in FIELD_NAMES.items():
                if before.get(field) != after.get(field):
                    changes.append({
                        "field": label,
                        "before": _field_text(field, before.get(field), before.get("currency"), account_names),
                        "after": _field_text(field, after.get(field), after.get("currency"), account_names),
                    })
        has_money = op.kind in TRADE_KINDS or op.kind in EDIT_KINDS
        sum_usd = (
            _units(Decimal(op.quantity * op.price) / op.fx_rate)
            if has_money and op.fx_rate and op.price is not None and op.quantity else None
        )
        active_target = op.kind in TRADE_KINDS and not op.cancelled and op.id in current
        target = by_id.get(op.target_id) if op.target_id else None
        rows.append({
            "id": op.id,
            "kind": op.kind,
            "kind_name": KIND_NAMES[op.kind],
            "op_date": op.op_date.isoformat(),
            "created_at": op.created_at,
            "item": op.market_hash_name,
            "icon": (items.get(op.market_hash_name) or {"icon_url": None})["icon_url"],
            "quantity": op.quantity,
            "price": _units(op.price) if op.kind != "transfer" else None,
            "currency": op.currency.name if op.currency is not None else None,
            "fx_rate": float(op.fx_rate) if op.fx_rate is not None and has_money else None,
            "fx_source": op.fx_source if has_money else None,
            "sum_usd": sum_usd,
            "account": account_names.get(op.account_id, "") if op.account_id else "",
            "to_account": account_names.get(op.to_account_id, "") if op.to_account_id else "",
            "comment": op.comment,
            "cancelled": op.cancelled,
            "history": history.get(op.id, []),
            "target_id": op.target_id,
            "target_kind": KIND_NAMES[target.kind] if target else None,
            "order_id": op.order_id,
            "edited_by": edited_by.get(op.id, []),
            "deleted_by": deleted_by.get(op.id, []),
            "changes": changes,
            "can_edit": active_target,
            "current": _op_values(current[op.id], account_names) if active_target else None,
        })
    rows.sort(key=lambda r: r["id"], reverse=True)
    return {"rows": rows, "accounts": account_names}


def preview(conn: sqlite3.Connection, result: Preview, ops: list[Op], portfolio_id: int = 1) -> dict[str, Any]:
    account_names = {a["id"]: a["name"] for a in accounts(conn, portfolio_id)}
    by_id = {op.id: op for op in ops}
    return {
        "allowed": result.allowed,
        "violations": result.violations,
        "positions": [
            {
                "item": c.key.market_hash_name,
                "account": account_names.get(c.key.account_id, "?"),
                "currency": c.key.currency.name,
                "quantity_before": c.quantity_before,
                "quantity_after": c.quantity_after,
                "avg_before": _units(c.avg_before, 4),
                "avg_after": _units(c.avg_after, 4),
            }
            for c in result.positions
        ],
        "sales": [
            {
                "op_id": s.op_id,
                "item": by_id[s.op_id].market_hash_name,
                "currency": by_id[s.op_id].currency.name,
                "profit_before": _units(s.profit_before),
                "profit_after": _units(s.profit_after),
                "profit_usd_before": _units(s.profit_usd_before),
                "profit_usd_after": _units(s.profit_usd_after),
            }
            for s in result.sales
        ],
    }


def orders(conn: sqlite3.Connection, ledger: Ledger, portfolio_id: int = 1) -> dict[str, Any]:
    prices = Prices(conn, datetime.now(UTC))
    rates = latest_rates(conn)
    account_names = {a["id"]: a["name"] for a in accounts(conn, portfolio_id)}
    open_orders = ledger.open_orders
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
