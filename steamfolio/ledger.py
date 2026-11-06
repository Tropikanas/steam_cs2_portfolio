"""Пересчёт портфеля и прибыли из записей покупок, продаж и переносов.

Чистые функции без базы: на вход — все записи портфеля, на выход — состояние.
Каждая покупка — своя строка портфеля; продажа и перенос списывают штуки с конкретной строки
(target_id продажи, payload["lot"] переноса). Записи правятся и удаляются на месте; порядок пересчёта —
по дате сделки, внутри дня — по порядку ввода.

Деньги — Decimal в сотых долях валюты (копейки, центы), курсы — единиц валюты за 1 USD.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from collections.abc import Callable
from typing import Any, NamedTuple

from steamfolio.currencies import Currency

# Поля покупки и продажи, которые можно изменить.
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
    position_no: int | None = None  # у покупки: «ID позиции» — номер строки портфеля

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
            position_no=row["position_no"],
        )


class RowKey(NamedTuple):
    """Строка портфеля — одна покупка (lot — её id): предмет в одной валюте на одном аккаунте.
    Покупки не складываются в одну строку; «Объединить строки» на сайте — только просмотр итогами."""

    market_hash_name: str
    account_id: int
    currency: Currency
    lot: int | None = None


@dataclass
class Position:
    key: RowKey
    quantity: int = 0  # сколько осталось из этой покупки
    cost: Decimal = Decimal(0)  # себестоимость остатка в валюте строки
    cost_usd: Decimal = Decimal(0)  # она же по курсу на дату покупки
    first_buy_date: date | None = None  # дата покупки
    bought: int = 0  # сколько куплено этой покупкой
    exited: bool = False  # было продано или перенесено

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
    key: RowKey  # строка (покупка), из которой продано
    quantity: int
    price: int
    avg_price: Decimal
    avg_price_usd: Decimal
    revenue_usd: Decimal
    profit: Decimal  # в валюте строки
    profit_usd: Decimal
    profit_pct: Decimal  # цена продажи / цена покупки − 1, в процентах
    months: Decimal  # от даты покупки до продажи
    first_buy_date: date | None = None

    @property
    def days_held(self) -> int:
        """Сколько дней держали; куплено и продано в один день — считаем за 1 день."""
        return max(1, (self.op_date - self.first_buy_date).days) if self.first_buy_date else 1

    @property
    def annual_pct(self) -> Decimal:
        """Простые проценты годовых: % прибыли × 365 / дней владения."""
        return self.profit_pct * 365 / self.days_held


@dataclass(frozen=True)
class Violation:
    op_id: int
    message: str


@dataclass
class Ledger:
    positions: dict[RowKey, Position] = field(default_factory=dict)
    sales: list[Sale] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    invested_usd: Decimal = Decimal(0)  # все покупки по курсу на дату покупки
    sold_usd: Decimal = Decimal(0)  # все продажи по курсу на дату продажи
    profit_usd: Decimal = Decimal(0)  # сумма прибыли по всем продажам
    lots: dict[int, RowKey] = field(default_factory=dict)  # id покупки → её строка

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.quantity > 0]

    @property
    def closed_positions(self) -> list[Position]:
        """Строки, проданные или перенесённые целиком."""
        return [p for p in self.positions.values() if p.quantity == 0 and p.exited]

    def lot(self, lot_id: int | None) -> Position | None:
        key = self.lots.get(lot_id)
        return self.positions.get(key) if key else None

    def total(self, item: str, account_id: int, currency: Currency) -> Position:
        """Все строки предмета в валюте на аккаунте вместе (для проверок и тестов)."""
        key = RowKey(item, account_id, currency)
        lots = [p for p in self.positions.values() if p.key[:3] == key[:3]]
        return Position(key, sum(p.quantity for p in lots), sum((p.cost for p in lots), Decimal(0)),
                        sum((p.cost_usd for p in lots), Decimal(0)),
                        min((p.first_buy_date for p in lots if p.quantity), default=None),
                        sum(p.bought for p in lots), any(p.exited for p in lots))


def lot_of(op: Op) -> int | None:
    """Из какой строки (покупки) продажа или перенос."""
    if op.kind == "sell":
        return op.target_id
    if op.kind == "transfer":
        return (op.payload or {}).get("lot")
    return op.id if op.kind == "buy" else None


def effective_ops(ops: list[Op]) -> list[Op]:
    """Действующие записи. Перенос — пара «списание со строки» + «покупка на новом аккаунте»:
    без одной половины нет и другой (удаление покупки отменяет перенос целиком)."""
    result = list(ops)
    splits = {op.id for op in result if is_split_transfer(op)}
    bought = {op.target_id for op in result if is_transfer_buy(op)}
    return [op for op in result
            if not (op.id in splits and op.id not in bought) and not (is_transfer_buy(op) and op.target_id not in splits)]


def is_split_transfer(op: Op) -> bool:
    """Перенос: списывает штуки со строки исходного аккаунта, на новый приходит связанная покупка."""
    return op.kind == "transfer" and bool((op.payload or {}).get("split"))


def is_transfer_buy(op: Op) -> bool:
    """Покупка, которой перенос привёз штуки на новый аккаунт (target_id — сам перенос)."""
    return op.kind == "buy" and op.target_id is not None


def replay(ops: list[Op]) -> Ledger:
    ledger, timeline = _prepare(ops)
    for op in timeline:
        _apply(ledger, op)
    return ledger


Holding = tuple[int, Decimal, Decimal]  # (количество, себестоимость в USD, себестоимость в валюте строки)


def daily_holdings(ops: list[Op]) -> list[tuple[date, dict[RowKey, Holding]]]:
    """Остатки на конец каждого дня с операциями: [(дата, {строка: (количество, себестоимость USD, в валюте)})].

    Покупка, привезённая переносом, датирована покупкой исходной строки, но до переноса эти штуки
    лежали на исходном аккаунте — на графике она приходит в день переноса, иначе штуки посчитались бы дважды.
    """
    clean = effective_ops(ops)
    transfers = {op.id: op for op in clean if is_split_transfer(op)}
    clean = [replace(op, op_date=transfers[op.target_id].op_date, created_at=transfers[op.target_id].created_at)
             if is_transfer_buy(op) and op.target_id in transfers and op.op_date < transfers[op.target_id].op_date
             else op for op in clean]
    ledger, timeline = _prepare(clean)
    result: list[tuple[date, dict[RowKey, Holding]]] = []
    for op in timeline:
        _apply(ledger, op)
        state = {k: (p.quantity, p.cost_usd, p.cost) for k, p in ledger.positions.items() if p.quantity > 0}
        if result and result[-1][0] == op.op_date:
            result[-1] = (op.op_date, state)
        else:
            result.append((op.op_date, state))
    while result and not result[0][1]:  # график — с первой покупки
        result.pop(0)
    return result


def _prepare(ops: list[Op]) -> tuple[Ledger, list[Op]]:
    current = effective_ops(ops)
    return Ledger(), sorted(current, key=timeline_key(current))


def timeline_key(current: list[Op]) -> Callable[[Op], tuple]:
    """Порядок пересчёта: по дате, внутри дня — по вводу. Покупка, привезённая переносом, датирована
    покупкой исходной строки, но применяется сразу после переноса (до него этих штук на аккаунте не было)."""
    transfers = {op.id: op for op in current if is_split_transfer(op)}

    def key(op: Op) -> tuple:
        t = transfers.get(op.target_id) if is_transfer_buy(op) else None
        if t is not None and op.sort_key < t.sort_key:
            return (*t.sort_key, 1)
        return (*op.sort_key, 0)

    return key


def _apply(ledger: Ledger, op: Op) -> None:
    if op.kind == "sell":
        _sell(ledger, op)
    elif op.kind == "transfer":
        _transfer(ledger, op)
    elif op.kind == "buy":
        _buy(ledger, op)


def _buy(ledger: Ledger, op: Op) -> None:
    key = RowKey(op.market_hash_name, op.account_id, op.currency, op.id)
    amount = Decimal(op.quantity * op.price)
    amount_usd = amount / op.fx_rate
    ledger.lots[op.id] = key
    ledger.positions[key] = Position(key, op.quantity, amount, amount_usd, op.op_date, op.quantity)
    ledger.invested_usd += amount_usd


def _source(ledger: Ledger, op: Op, what: str) -> Position | None:
    """Строка, из которой продают или переносят; нет её или не хватает штук — нарушение."""
    pos = ledger.lot(lot_of(op))
    have = pos.quantity if pos else 0
    if pos is None or op.quantity > have:
        place = f"«{op.market_hash_name}» {op.op_date:%d.%m.%Y}"
        message = (f"{what} {op.quantity} шт. {place}: строки, из которой {what}, нет" if pos is None
                   else f"{what} {op.quantity} шт. {place} при остатке {have} шт.")
        ledger.violations.append(Violation(op.id, message))
        return None
    return pos


def _transfer(ledger: Ledger, op: Op) -> None:
    """Перенос части строки на другой аккаунт: списание по цене покупки (её принесёт связанная покупка)."""
    src = _source(ledger, op, "перенос")
    if src is None:
        return
    avg, avg_usd = src.avg_price, src.avg_price_usd
    ledger.invested_usd -= avg_usd * op.quantity
    _take_out(src, op.quantity, avg, avg_usd)


def _take_out(src: Position, quantity: int, avg: Decimal, avg_usd: Decimal) -> None:
    src.quantity -= quantity
    src.exited = True
    if src.quantity == 0:
        src.cost = src.cost_usd = Decimal(0)
    else:
        src.cost -= avg * quantity
        src.cost_usd -= avg_usd * quantity


def _sell(ledger: Ledger, op: Op) -> None:
    pos = _source(ledger, op, "продажа")
    if pos is None:
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
            key=pos.key,
            quantity=op.quantity,
            price=op.price,
            avg_price=avg,
            avg_price_usd=avg_usd,
            revenue_usd=revenue_usd,
            profit=profit,
            profit_usd=profit_usd,
            profit_pct=(op.price / avg - 1) * 100 if avg else Decimal(0),
            months=(days / DAYS_PER_MONTH).quantize(Decimal("0.1"), ROUND_HALF_UP),
            first_buy_date=pos.first_buy_date,
        )
    )
    ledger.sold_usd += revenue_usd
    ledger.profit_usd += profit_usd
    _take_out(pos, op.quantity, avg, avg_usd)


