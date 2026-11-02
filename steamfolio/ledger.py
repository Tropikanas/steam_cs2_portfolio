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
from collections.abc import Callable
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
    # С последнего открытия с нуля: были ли продажи или переносы на другой аккаунт,
    # и закрыта ли позиция удалением. Удалённая без продаж и переносов — не «закрытая», её будто не было.
    exited: bool = False
    removed: bool = False

    def reopen(self, day: date) -> None:
        self.cost = self.cost_usd = Decimal(0)
        self.first_buy_date = day
        self.exited = self.removed = False

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
    first_buy_date: date | None = None

    @property
    def days_held(self) -> int:
        """Сколько дней держали; куплено и продано в один день — считаем за 1 день."""
        return max(1, (self.op_date - self.first_buy_date).days) if self.first_buy_date else 1

    @property
    def annual_pct(self) -> Decimal:
        """Простые проценты годовых: % прибыли × 365 / дней владения."""
        return self.profit_pct * 365 / self.days_held


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
        """Проданные или перенесённые целиком; удалённые как ошибочные сюда не входят."""
        return [p for p in self.positions.values() if p.quantity == 0 and not (p.removed and not p.exited)]

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
    # Перенос — пара «списание с аккаунта» + «покупка на новом аккаунте»: без одной половины нет и другой
    # (откат или удаление любой из них отменяет перенос целиком).
    splits = {op.id for op in result if is_split_transfer(op)}
    bought = {op.target_id for op in result if is_transfer_buy(op)}
    return [op for op in result
            if not (op.id in splits and op.id not in bought) and not (is_transfer_buy(op) and op.target_id not in splits)]


def is_split_transfer(op: Op) -> bool:
    """Перенос нового вида: только списывает с исходного аккаунта, на новый приходит связанная покупка."""
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


def removal_matches(ops: list[Op]) -> tuple[list[Op], dict[int, int], set[int]]:
    """Какие поступления сняты удалением позиций: (действующие операции, {id поступления: снято штук}, id удалений).

    Удалённая позиция — ошибочно внесённые штуки. Это последние пришедшие в строку штуки
    (покупки, исполнения ордеров, переносы на этот аккаунт), ещё не снятые прежними удалениями:
    одну покупку нельзя «удалить» дважды. Пришедшие переносом снимаются и с исходного аккаунта.
    """
    ledger, timeline = _prepare(ops)
    valid: list[int] = []
    for index, op in enumerate(timeline):
        problems = len(ledger.violations)
        _apply(ledger, op)
        if op.kind == "position_removed" and len(ledger.violations) == problems:
            valid.append(index)
    taken: dict[int, int] = {}
    position = {op.id: i for i, op in enumerate(timeline)}

    def take(index: int, key: RowKey, count: int) -> None:
        for i in range(index - 1, -1, -1):
            if count <= 0:
                return
            op = timeline[i]
            if not _brings_into(op, key):
                continue
            got = min(count, op.quantity - taken.get(op.id, 0))
            if got <= 0:
                continue
            taken[op.id] = taken.get(op.id, 0) + got
            if op.kind == "transfer":
                take(i, RowKey(op.market_hash_name, op.account_id, op.currency), got)
            elif is_transfer_buy(op) and op.target_id in position:
                # Привезены переносом: до переноса эти штуки были на исходном аккаунте — снимаем и там,
                # а перенос становится меньше на столько же.
                t = position[op.target_id]
                moved = timeline[t]
                taken[moved.id] = taken.get(moved.id, 0) + got
                take(t, RowKey(moved.market_hash_name, moved.account_id, moved.currency), got)
            count -= got

    for index in valid:
        op = timeline[index]
        take(index, RowKey(op.market_hash_name, op.account_id, op.currency), op.quantity)
    return timeline, taken, {timeline[index].id for index in valid}


def without_removed(ops: list[Op]) -> list[Op]:
    """Журнал без ошибочно внесённого: снятые удалением штуки вычтены из своих поступлений, удаления убраны.
    Остаются ровно те покупки, из которых сложились текущий портфель и закрытые позиции."""
    _, taken, removals = removal_matches(ops)
    result = []
    for op in effective_ops(ops):  # с выставлениями ордеров: без них исполнения не засчитаются
        if op.id in removals:
            continue
        if op.id in taken:
            if op.quantity <= taken[op.id]:
                continue
            op = replace(op, quantity=op.quantity - taken[op.id])
        result.append(op)
    return result


def daily_holdings(ops: list[Op]) -> list[tuple[date, dict[RowKey, Holding]]]:
    """Остатки на конец каждого дня с операциями: [(дата, {строка: (количество, себестоимость USD, в валюте)})].

    Строится по журналу без ошибочно внесённого (without_removed): удалённых штук нет и в прошлом.
    Покупка, привезённая переносом, датирована первой покупкой исходной строки, но до переноса эти штуки
    лежали на исходном аккаунте — на графике она приходит в день переноса, иначе штуки посчитались бы дважды.
    """
    clean = without_removed(ops)
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
    # Если удалено всё, что было куплено в начале, график начинается с первой оставшейся покупки.
    while result and not result[0][1]:
        result.pop(0)
    return result


def _brings_into(op: Op, key: RowKey) -> bool:
    """Операция добавляет штуки в строку key: покупка, исполнение ордера или перенос на этот аккаунт."""
    if op.market_hash_name != key.market_hash_name or op.currency != key.currency:
        return False
    if op.kind == "transfer":
        return not is_split_transfer(op) and op.to_account_id == key.account_id  # новый вид привозит покупкой
    return op.kind in ("buy", "order_filled") and op.account_id == key.account_id


def _prepare(ops: list[Op]) -> tuple[Ledger, list[Op]]:
    ledger = Ledger()
    current = effective_ops(ops)
    # Ордер видит исполнение таким, каким оно было внесено: дальнейшие правки покупки (количество, цена,
    # предмет, валюта, аккаунт) меняют только портфель, не остаток ордера.
    as_entered = {op.id: op for op in ops}
    order_ops = [as_entered[op.id] if op.kind == "order_filled" else op for op in current if op.kind.startswith("order_")]
    valid_fills = _replay_orders(ledger, sorted(order_ops, key=lambda o: o.entry_key))
    timeline = [op for op in current if op.kind in ("buy", "sell", "transfer", "position_removed") or op.id in valid_fills]
    return ledger, sorted(timeline, key=timeline_key(current))


def timeline_key(current: list[Op]) -> Callable[[Op], tuple]:
    """Порядок пересчёта: по дате, внутри дня — по вводу. Покупка, привезённая переносом, датирована первой
    покупкой исходной строки, но применяется сразу после переноса — иначе она смешалась бы с прежней
    историей строки на новом аккаунте (продажами и удалениями до переноса)."""
    transfers = {op.id: op for op in current if is_split_transfer(op)}

    def key(op: Op) -> tuple:
        t = transfers.get(op.target_id) if is_transfer_buy(op) else None
        if t is not None and op.sort_key < t.sort_key:
            return (*t.sort_key, 1)
        return (*op.sort_key, 0)

    return key


def timeline_order(ops: list[Op]) -> list[Op]:
    """Действующие операции в порядке пересчёта (см. timeline_key)."""
    current = effective_ops(ops)
    return sorted(current, key=timeline_key(current))


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
            order.account_id, order.currency = op.account_id, op.currency
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
        pos.reopen(op.op_date)
    elif is_transfer_buy(op):  # привезено в открытую строку — первая покупка самая ранняя из двух, как раньше
        pos.first_buy_date = min(pos.first_buy_date, op.op_date)
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
    if is_split_transfer(op):
        # Списание по средней цене; себестоимость на новом аккаунте принесла связанная покупка.
        ledger.invested_usd -= avg_usd * op.quantity
        _take_out(src, op.quantity, avg, avg_usd)
        return
    dst_key = RowKey(op.market_hash_name, op.to_account_id, op.currency)
    dst = ledger.positions.setdefault(dst_key, Position(dst_key))
    if dst.quantity == 0:
        dst.reopen(src.first_buy_date)
    else:
        dst.first_buy_date = min(dst.first_buy_date, src.first_buy_date)
    dst.quantity += op.quantity
    dst.cost += avg * op.quantity
    dst.cost_usd += avg_usd * op.quantity
    _take_out(src, op.quantity, avg, avg_usd)


def _take_out(src: Position, quantity: int, avg: Decimal, avg_usd: Decimal) -> None:
    """Штуки ушли с аккаунта переносом: остаток — по прежней средней цене."""
    src.quantity -= quantity
    src.exited = True
    if src.quantity == 0:
        src.cost = src.cost_usd = Decimal(0)
    else:
        src.cost -= avg * quantity
        src.cost_usd -= avg_usd * quantity


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
        pos.removed = True
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
            first_buy_date=pos.first_buy_date,
        )
    )
    ledger.sold_usd += revenue_usd
    ledger.profit_usd += profit_usd
    pos.quantity -= op.quantity
    pos.exited = True
    if pos.quantity == 0:
        pos.cost = pos.cost_usd = Decimal(0)
    else:
        pos.cost -= avg * op.quantity
        pos.cost_usd -= avg_usd * op.quantity
