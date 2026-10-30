"""Пересчёт портфеля, ордеров и прибыли из журнала операций.

Чистые функции без базы: на вход — все операции портфеля, на выход — состояние.

Порядок пересчёта:
- позиции — по дате сделки, внутри дня — по порядку ввода;
- ордера — по порядку ввода (у выставления ордера нет даты сделки, исполнение
  может быть внесено «задним числом»).

Деньги — Decimal в сотых долях валюты (копейки, центы), курсы — единиц валюты за 1 USD.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, NamedTuple

from steamfolio.currencies import Currency

EDIT_KINDS = {"buy_edited", "sell_edited"}
DELETE_KINDS = {"buy_deleted", "sell_deleted"}
# Поля покупки/продажи, которые можно изменить через журнал.
EDITABLE_FIELDS = ("op_date", "market_hash_name", "account_id", "currency", "quantity", "price", "fx_rate", "fx_source", "comment")
DAYS_PER_MONTH = Decimal("30.4375")


@dataclass(frozen=True)
class Op:
    id: int
    kind: str
    op_date: date
    created_at: str
    market_hash_name: str | None = None
    account_id: int | None = None
    currency: Currency | None = None
    quantity: int | None = None
    price: int | None = None
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    order_id: int | None = None
    target_id: int | None = None
    payload: dict[str, Any] | None = None
    comment: str = ""
    cancelled: bool = False
    to_account_id: int | None = None  # для переноса: куда (account_id — откуда)

    @property
    def sort_key(self) -> tuple:
        return (self.op_date, self.created_at, self.id)

    @property
    def entry_key(self) -> tuple:
        return (self.created_at, self.id)

    def fields(self) -> dict[str, Any]:
        """Изменяемые поля в виде, пригодном для JSON."""
        return {
            "op_date": self.op_date.isoformat(),
            "market_hash_name": self.market_hash_name,
            "account_id": self.account_id,
            "currency": int(self.currency) if self.currency is not None else None,
            "quantity": self.quantity,
            "price": self.price,
            "fx_rate": str(self.fx_rate) if self.fx_rate is not None else None,
            "fx_source": self.fx_source,
            "comment": self.comment,
        }

    def with_fields(self, values: dict[str, Any]) -> Op:
        changes = {k: v for k, v in values.items() if k in EDITABLE_FIELDS}
        if "op_date" in changes:
            changes["op_date"] = date.fromisoformat(changes["op_date"])
        if changes.get("currency") is not None:
            changes["currency"] = Currency(changes["currency"])
        if changes.get("fx_rate") is not None:
            changes["fx_rate"] = Decimal(changes["fx_rate"])
        return replace(self, **changes)

    @classmethod
    def from_row(cls, row: Any) -> Op:
        return cls(
            id=row["id"],
            kind=row["kind"],
            op_date=date.fromisoformat(row["op_date"]),
            created_at=row["created_at"],
            market_hash_name=row["market_hash_name"],
            account_id=row["account_id"],
            currency=Currency(row["currency"]) if row["currency"] is not None else None,
            quantity=row["quantity"],
            price=row["price"],
            fx_rate=Decimal(row["fx_rate"]) if row["fx_rate"] is not None else None,
            fx_source=row["fx_source"],
            order_id=row["order_id"],
            target_id=row["target_id"],
            payload=json.loads(row["payload"]) if row["payload"] else None,
            comment=row["comment"],
            cancelled=row["cancelled_at"] is not None,
            to_account_id=row["to_account_id"],
        )


class RowKey(NamedTuple):
    """Строка портфеля: предмет в одной валюте на одном аккаунте."""

    market_hash_name: str
    account_id: int
    currency: Currency


@dataclass
class Position:
    key: RowKey
    quantity: int = 0
    cost: Decimal = Decimal(0)  # себестоимость остатка в валюте строки
    cost_usd: Decimal = Decimal(0)  # она же по курсам на даты покупок
    first_buy_date: date | None = None  # с последнего открытия позиции с нуля

    @property
    def avg_price(self) -> Decimal | None:
        return self.cost / self.quantity if self.quantity else None

    @property
    def avg_price_usd(self) -> Decimal | None:
        return self.cost_usd / self.quantity if self.quantity else None


@dataclass(frozen=True)
class Sale:
    op_id: int
    op_date: date
    key: RowKey
    quantity: int
    price: int
    avg_price: Decimal
    avg_price_usd: Decimal
    revenue_usd: Decimal
    profit: Decimal  # в валюте строки
    profit_usd: Decimal
    profit_pct: Decimal  # цена продажи / средняя − 1, в процентах
    months: Decimal  # от самой ранней даты покупки позиции до продажи


@dataclass
class OrderState:
    order_id: int
    market_hash_name: str
    account_id: int
    currency: Currency
    price: int
    quantity: int  # сколько ещё не исполнено
    comment: str
    status: str = "open"  # open | filled | removed

    @property
    def total(self) -> int:
        return self.price * self.quantity


@dataclass(frozen=True)
class Violation:
    op_id: int
    message: str


@dataclass
class Ledger:
    positions: dict[RowKey, Position] = field(default_factory=dict)
    orders: dict[int, OrderState] = field(default_factory=dict)
    sales: list[Sale] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    invested_usd: Decimal = Decimal(0)  # все покупки по курсу на дату покупки
    sold_usd: Decimal = Decimal(0)  # все продажи по курсу на дату продажи
    profit_usd: Decimal = Decimal(0)  # сумма прибыли по всем продажам

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.quantity > 0]

    @property
    def closed_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.quantity == 0]

    @property
    def open_orders(self) -> list[OrderState]:
        return [o for o in self.orders.values() if o.status == "open"]


def effective_ops(ops: list[Op]) -> list[Op]:
    """Действующие операции: без откаченных, с применёнными изменениями и удалениями."""
    active = [op for op in ops if not op.cancelled]
    last_edit: dict[int, Op] = {}
    for op in sorted((op for op in active if op.kind in EDIT_KINDS), key=lambda o: o.entry_key):
        last_edit[op.target_id] = op
    deleted = {op.target_id for op in active if op.kind in DELETE_KINDS}
    result = []
    for op in active:
        if op.kind in EDIT_KINDS or op.kind in DELETE_KINDS or op.id in deleted:
            continue
        if op.id in last_edit:
            op = op.with_fields(last_edit[op.id].payload["after"])
        result.append(op)
    return result


def replay(ops: list[Op]) -> Ledger:
    ledger, timeline = _prepare(ops)
    for op in timeline:
        _apply(ledger, op)
    return ledger


Holding = tuple[int, Decimal, Decimal]  # (количество, себестоимость в USD, себестоимость в валюте строки)


def daily_holdings(ops: list[Op]) -> list[tuple[date, dict[RowKey, Holding]]]:
    """Остатки на конец каждого дня с операциями: [(дата, {строка: (количество, себестоимость USD, в валюте)})].

    Удалённая позиция считается ошибочно внесённой: её штуки убираются и из прошлого,
    будто их никогда не было (остаток строки до даты удаления уменьшается на удалённое количество).
    """
    ledger, timeline = _prepare(ops)
    result: list[tuple[date, dict[RowKey, Holding]]] = []
    removals: list[tuple[date, RowKey, int]] = []
    for op in timeline:
        problems = len(ledger.violations)
        _apply(ledger, op)
        if op.kind == "position_removed" and len(ledger.violations) == problems:
            removals.append((op.op_date, RowKey(op.market_hash_name, op.account_id, op.currency), op.quantity))
        state = {k: (p.quantity, p.cost_usd, p.cost) for k, p in ledger.positions.items() if p.quantity > 0}
        if result and result[-1][0] == op.op_date:
            result[-1] = (op.op_date, state)
        else:
            result.append((op.op_date, state))
    for removed_on, key, removed in removals:
        for day, state in result:
            if day >= removed_on or key not in state:
                continue
            qty, cost_usd, cost = state[key]
            left = qty - removed
            if left > 0:
                state[key] = (left, cost_usd * left / qty, cost * left / qty)  # по средней цене
            else:
                del state[key]
    # Если удалено всё, что было куплено в начале, график начинается с первой оставшейся покупки.
    while result and not result[0][1]:
        result.pop(0)
    return result


def _prepare(ops: list[Op]) -> tuple[Ledger, list[Op]]:
    ledger = Ledger()
    current = effective_ops(ops)
    valid_fills = _replay_orders(ledger, sorted((op for op in current if op.kind.startswith("order_")), key=lambda o: o.entry_key))
    timeline = [op for op in current if op.kind in ("buy", "sell", "transfer", "position_removed") or op.id in valid_fills]
    return ledger, sorted(timeline, key=lambda o: o.sort_key)


def _apply(ledger: Ledger, op: Op) -> None:
    if op.kind == "sell":
        _sell(ledger, op)
    elif op.kind == "transfer":
        _transfer(ledger, op)
    elif op.kind == "position_removed":
        _remove(ledger, op)
    else:
        _buy(ledger, op)


def _replay_orders(ledger: Ledger, ops: list[Op]) -> set[int]:
    valid_fills: set[int] = set()
    for op in ops:
        if op.kind == "order_placed":
            ledger.orders[op.id] = OrderState(
                op.id, op.market_hash_name, op.account_id, op.currency, op.price, op.quantity, op.comment
            )
            continue
        order = ledger.orders.get(op.order_id)
        if order is None or order.status != "open":
            state = "не выставлен" if order is None else ("уже исполнен" if order.status == "filled" else "убран")
            ledger.violations.append(Violation(op.id, f"ордер #{op.order_id} {state}"))
            continue
        if op.kind == "order_changed":
            order.price, order.quantity, order.comment = op.price, op.quantity, op.comment
        elif op.kind == "order_removed":
            order.status = "removed"
        elif op.kind == "order_filled":
            if op.quantity > order.quantity:
                ledger.violations.append(
                    Violation(op.id, f"исполнение {op.quantity} шт. больше остатка ордера #{order.order_id} ({order.quantity} шт.)")
                )
                continue
            order.quantity -= op.quantity
            if order.quantity == 0:
                order.status = "filled"
            valid_fills.add(op.id)
    return valid_fills


def _buy(ledger: Ledger, op: Op) -> None:
    key = RowKey(op.market_hash_name, op.account_id, op.currency)
    pos = ledger.positions.setdefault(key, Position(key))
    if pos.quantity == 0:
        pos.cost = pos.cost_usd = Decimal(0)
        pos.first_buy_date = op.op_date
    amount = Decimal(op.quantity * op.price)
    amount_usd = amount / op.fx_rate
    pos.quantity += op.quantity
    pos.cost += amount
    pos.cost_usd += amount_usd
    ledger.invested_usd += amount_usd


def _transfer(ledger: Ledger, op: Op) -> None:
    """Перенос части строки на другой аккаунт: себестоимость переезжает по средней цене, прибыли нет."""
    src_key = RowKey(op.market_hash_name, op.account_id, op.currency)
    src = ledger.positions.get(src_key)
    have = src.quantity if src else 0
    if op.quantity > have:
        ledger.violations.append(
            Violation(op.id, f"перенос {op.quantity} шт. «{op.market_hash_name}» {op.op_date:%d.%m.%Y} при остатке {have} шт.")
        )
        return
    avg, avg_usd = src.avg_price, src.avg_price_usd
    dst_key = RowKey(op.market_hash_name, op.to_account_id, op.currency)
    dst = ledger.positions.setdefault(dst_key, Position(dst_key))
    if dst.quantity == 0:
        dst.cost = dst.cost_usd = Decimal(0)
        dst.first_buy_date = src.first_buy_date
    else:
        dst.first_buy_date = min(dst.first_buy_date, src.first_buy_date)
    dst.quantity += op.quantity
    dst.cost += avg * op.quantity
    dst.cost_usd += avg_usd * op.quantity
    src.quantity -= op.quantity
    if src.quantity == 0:
        src.cost = src.cost_usd = Decimal(0)
    else:
        src.cost -= avg * op.quantity
        src.cost_usd -= avg_usd * op.quantity


def _remove(ledger: Ledger, op: Op) -> None:
    """Удаление позиции как ошибочно внесённой: остаток уходит по средней цене, будто его не покупали.

    «Всего вложено» уменьшается на его себестоимость в USD, прибыли и убытка нет.
    """
    key = RowKey(op.market_hash_name, op.account_id, op.currency)
    pos = ledger.positions.get(key)
    have = pos.quantity if pos else 0
    if op.quantity > have:
        ledger.violations.append(
            Violation(op.id, f"удаление {op.quantity} шт. «{op.market_hash_name}» {op.op_date:%d.%m.%Y} при остатке {have} шт.")
        )
        return
    avg, avg_usd = pos.avg_price, pos.avg_price_usd
    ledger.invested_usd -= avg_usd * op.quantity
    pos.quantity -= op.quantity
    if pos.quantity == 0:
        pos.cost = pos.cost_usd = Decimal(0)
    else:
        pos.cost -= avg * op.quantity
        pos.cost_usd -= avg_usd * op.quantity


def _sell(ledger: Ledger, op: Op) -> None:
    key = RowKey(op.market_hash_name, op.account_id, op.currency)
    pos = ledger.positions.get(key)
    have = pos.quantity if pos else 0
    if op.quantity > have:
        ledger.violations.append(
            Violation(op.id, f"продажа {op.quantity} шт. «{op.market_hash_name}» {op.op_date:%d.%m.%Y} при остатке {have} шт.")
        )
        return
    avg, avg_usd = pos.avg_price, pos.avg_price_usd
    revenue_usd = Decimal(op.quantity * op.price) / op.fx_rate
    profit = (op.price - avg) * op.quantity
    profit_usd = revenue_usd - avg_usd * op.quantity
    days = Decimal((op.op_date - pos.first_buy_date).days)
    ledger.sales.append(
        Sale(
            op_id=op.id,
            op_date=op.op_date,
            key=key,
            quantity=op.quantity,
            price=op.price,
            avg_price=avg,
            avg_price_usd=avg_usd,
            revenue_usd=revenue_usd,
            profit=profit,
            profit_usd=profit_usd,
            profit_pct=(op.price / avg - 1) * 100 if avg else Decimal(0),
            months=(days / DAYS_PER_MONTH).quantize(Decimal("0.1"), ROUND_HALF_UP),
        )
    )
    ledger.sold_usd += revenue_usd
    ledger.profit_usd += profit_usd
    pos.quantity -= op.quantity
    if pos.quantity == 0:
        pos.cost = pos.cost_usd = Decimal(0)
    else:
        pos.cost -= avg * op.quantity
        pos.cost_usd -= avg_usd * op.quantity
