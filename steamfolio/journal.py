"""Журнал операций: запись, изменение, откат — с проверкой через полный пересчёт.

Любое действие сначала применяется к копии журнала в памяти. Если пересчёт находит
нарушения (продажа больше остатка, исполнение убранного ордера и т. п.), действие
отклоняется с перечнем операций, которые мешают.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from steamfolio.clock import today_msk
from steamfolio.currencies import USER_CURRENCIES, Currency
from steamfolio.db import utcnow
from steamfolio.fx import FxResolver
from steamfolio.ledger import EDITABLE_FIELDS, Ledger, Op, OrderState, RowKey, effective_ops, replay

# Временный id ещё не записанной операции: больше любого настоящего.
PENDING_ID = 10**15

KIND_NAMES = {
    "buy": "покупка",
    "sell": "продажа",
    "order_placed": "ордер выставлен",
    "order_changed": "ордер изменён",
    "order_filled": "ордер исполнен",
    "order_removed": "ордер убран",
    "buy_edited": "покупка изменена",
    "buy_deleted": "покупка удалена",
    "sell_edited": "продажа изменена",
    "sell_deleted": "продажа удалена",
    "transfer": "перенос на аккаунт",
}
# Поля исполнения ордера, которые можно изменить: предмет, валюта и аккаунт задаются ордером.
FILL_EDITABLE_FIELDS = ("op_date", "quantity", "price", "fx_rate", "fx_source", "comment")


class JournalError(Exception):
    """Действие отклонено. violations — что мешает, по одной строке на операцию."""

    def __init__(self, message: str, violations: list[str] | None = None):
        super().__init__(message if not violations else message + ":\n" + "\n".join(violations))
        self.violations = violations or []


@dataclass(frozen=True)
class PositionChange:
    key: RowKey
    quantity_before: int
    quantity_after: int
    avg_before: Decimal | None
    avg_after: Decimal | None


@dataclass(frozen=True)
class SaleChange:
    op_id: int
    profit_before: Decimal | None  # None — продажи не было
    profit_after: Decimal | None  # None — продажи не станет
    profit_usd_before: Decimal | None
    profit_usd_after: Decimal | None


@dataclass(frozen=True)
class Preview:
    """Что изменится после отката или его отмены."""

    positions: list[PositionChange]
    sales: list[SaleChange]
    violations: list[str]

    @property
    def allowed(self) -> bool:
        return not self.violations


class Journal:
    def __init__(self, conn: sqlite3.Connection, fx: FxResolver, portfolio_id: int = 1, user_id: int | None = 1):
        self.conn = conn
        self.fx = fx
        self.portfolio_id = portfolio_id
        self.user_id = user_id

    # ---------- чтение ----------

    def ops(self) -> list[Op]:
        rows = self.conn.execute("SELECT * FROM operations WHERE portfolio_id = ? ORDER BY id", (self.portfolio_id,))
        return [Op.from_row(r) for r in rows]

    def ledger(self) -> Ledger:
        return replay(self.ops())

    def op(self, op_id: int) -> Op:
        row = self.conn.execute(
            "SELECT * FROM operations WHERE id = ? AND portfolio_id = ?", (op_id, self.portfolio_id)
        ).fetchone()
        if row is None:
            raise JournalError(f"операция #{op_id} не найдена")
        return Op.from_row(row)

    # ---------- покупки и продажи ----------

    def buy(
        self, item: str, account_id: int, currency: Currency, quantity: int, price: int,
        op_date: date | None = None, comment: str = "", fx_rate: Decimal | None = None,
    ) -> int:
        op_date = op_date or today_msk()
        self._check_trade(item, account_id, currency, quantity, price, op_date)
        rate, source = self._rate(currency, op_date, fx_rate)
        return self._add(Op(PENDING_ID, "buy", op_date, utcnow(), item, account_id, currency, quantity, price,
                            rate, source, comment=comment))

    def sell(
        self, item: str, account_id: int, currency: Currency, quantity: int, price: int,
        op_date: date | None = None, comment: str = "", fx_rate: Decimal | None = None,
    ) -> int:
        """price — чистая цена за штуку после комиссии, в валюте строки."""
        op_date = op_date or today_msk()
        self._check_trade(item, account_id, currency, quantity, price, op_date)
        rate, source = self._rate(currency, op_date, fx_rate)
        return self._add(Op(PENDING_ID, "sell", op_date, utcnow(), item, account_id, currency, quantity, price,
                            rate, source, comment=comment))

    def transfer(
        self, item: str, from_account_id: int, to_account_id: int, currency: Currency, quantity: int,
        op_date: date | None = None, comment: str = "",
    ) -> int:
        """Перенести часть строки на другой аккаунт. Средняя цена и дата первой покупки переезжают вместе с предметами."""
        op_date = op_date or today_msk()
        self._check_trade(item, from_account_id, currency, quantity, 0, op_date)
        self._check_account(to_account_id)
        if to_account_id == from_account_id:
            raise JournalError("аккаунты «откуда» и «куда» совпадают")
        return self._add(Op(PENDING_ID, "transfer", op_date, utcnow(), item, from_account_id, currency, quantity,
                            comment=comment, to_account_id=to_account_id))

    def edit(self, op_id: int, **changes) -> int:
        """Изменить покупку, продажу или исполнение ордера. Пишется отдельной операцией «было → стало»."""
        target = self._current(op_id)
        allowed = FILL_EDITABLE_FIELDS if target.kind == "order_filled" else EDITABLE_FIELDS
        unknown = set(changes) - set(allowed)
        if unknown:
            raise JournalError(f"нельзя изменить поля: {', '.join(sorted(unknown))}")
        if "currency" in changes:
            changes["currency"] = Currency(changes["currency"])
        if "fx_rate" in changes:
            changes["fx_rate"] = Decimal(changes["fx_rate"])
            changes["fx_source"] = "manual"
        after = replace(target, **changes)
        self._check_trade(after.market_hash_name, after.account_id, after.currency, after.quantity, after.price,
                          after.op_date)
        # Дата или валюта изменились, а курс не задан вручную — берём курс на новую дату.
        if "fx_rate" not in changes and after.fx_source != "manual" and (
            after.op_date != target.op_date or after.currency != target.currency
        ):
            rate, source = self._rate(after.currency, after.op_date, None)
            after = replace(after, fx_rate=rate, fx_source=source)
        before_fields, after_fields = target.fields(), after.fields()
        if before_fields == after_fields:
            raise JournalError("ничего не изменилось")
        kind = "sell_edited" if target.kind == "sell" else "buy_edited"
        return self._add(Op(PENDING_ID, kind, after.op_date, utcnow(), after.market_hash_name, after.account_id,
                            after.currency, after.quantity, after.price, after.fx_rate, after.fx_source,
                            target_id=op_id, payload={"before": before_fields, "after": after_fields},
                            comment=after.comment))

    def delete(self, op_id: int) -> int:
        """Удалить покупку, продажу или исполнение ордера (отдельной операцией, её можно откатить)."""
        target = self._current(op_id)
        kind = "sell_deleted" if target.kind == "sell" else "buy_deleted"
        return self._add(Op(PENDING_ID, kind, target.op_date, utcnow(), target.market_hash_name, target.account_id,
                            target.currency, target.quantity, target.price, target.fx_rate, target.fx_source,
                            target_id=op_id, payload={"before": target.fields()}))

    # ---------- ордера ----------

    def place_order(self, item: str, account_id: int, currency: Currency, quantity: int, price: int,
                    comment: str = "") -> int:
        today = today_msk()
        self._check_trade(item, account_id, currency, quantity, price, today)
        return self._add(Op(PENDING_ID, "order_placed", today, utcnow(), item, account_id, currency, quantity, price,
                            comment=comment))

    def change_order(self, order_id: int, price: int, quantity: int, comment: str | None = None) -> int:
        order = self._open_order(order_id)
        self._check_amounts(quantity, price)
        return self._add(Op(PENDING_ID, "order_changed", today_msk(), utcnow(), order.market_hash_name,
                            order.account_id, order.currency, quantity, price, order_id=order_id,
                            payload={"before": {"price": order.price, "quantity": order.quantity, "comment": order.comment}},
                            comment=order.comment if comment is None else comment))

    def fill_order(self, order_id: int, quantity: int | None = None, op_date: date | None = None,
                   price: int | None = None, fx_rate: Decimal | None = None) -> int:
        """Исполнение ордера. quantity=None — «Куплено всё»; price=None — по цене ордера."""
        order = self._open_order(order_id)
        quantity = order.quantity if quantity is None else quantity
        price = order.price if price is None else price
        op_date = op_date or today_msk()
        self._check_trade(order.market_hash_name, order.account_id, order.currency, quantity, price, op_date)
        rate, source = self._rate(order.currency, op_date, fx_rate)
        return self._add(Op(PENDING_ID, "order_filled", op_date, utcnow(), order.market_hash_name, order.account_id,
                            order.currency, quantity, price, rate, source, order_id=order_id))

    def remove_order(self, order_id: int) -> int:
        order = self._open_order(order_id)
        return self._add(Op(PENDING_ID, "order_removed", today_msk(), utcnow(), order.market_hash_name,
                            order.account_id, order.currency, order.quantity, order.price, order_id=order_id))

    # ---------- откат ----------

    def preview_cancel(self, op_id: int) -> Preview:
        return self._preview(op_id, cancelled=True)

    def preview_restore(self, op_id: int) -> Preview:
        return self._preview(op_id, cancelled=False)

    def cancel(self, op_id: int) -> Preview:
        return self._toggle(op_id, cancelled=True)

    def restore(self, op_id: int) -> Preview:
        return self._toggle(op_id, cancelled=False)

    # ---------- внутреннее ----------

    def _preview(self, op_id: int, cancelled: bool) -> Preview:
        op = self.op(op_id)
        if op.cancelled == cancelled:
            raise JournalError(f"операция #{op_id} уже {'отменена' if cancelled else 'действует'}")
        ops = self.ops()
        before = replay(ops)
        after = replay([replace(o, cancelled=cancelled) if o.id == op_id else o for o in ops])
        return _diff(before, after, self._describe_violations(after, before))

    def _toggle(self, op_id: int, cancelled: bool) -> Preview:
        preview = self._preview(op_id, cancelled)
        if not preview.allowed:
            action = "откатить" if cancelled else "вернуть"
            raise JournalError(f"нельзя {action} операцию #{op_id}, сначала откатите", preview.violations)
        now = utcnow()
        with self.conn:
            self.conn.execute("UPDATE operations SET cancelled_at = ? WHERE id = ?", (now if cancelled else None, op_id))
            self.conn.execute(
                "INSERT INTO operation_cancellations (operation_id, action, user_id, at) VALUES (?, ?, ?, ?)",
                (op_id, "cancel" if cancelled else "restore", self.user_id, now),
            )
        return preview

    def _add(self, op: Op) -> int:
        ops = self.ops()
        before = replay(ops)
        after = replay(ops + [op])
        problems = self._describe_violations(after, before)
        if problems:
            raise JournalError(f"{KIND_NAMES[op.kind]} не записана: противоречит журналу", problems)
        with self.conn:
            return self.conn.execute(
                """INSERT INTO operations (portfolio_id, user_id, kind, op_date, created_at, market_hash_name,
                       account_id, currency, quantity, price, fx_rate, fx_source, order_id, target_id, payload, comment,
                       to_account_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.portfolio_id, self.user_id, op.kind, op.op_date.isoformat(), op.created_at,
                    op.market_hash_name, op.account_id, int(op.currency) if op.currency is not None else None,
                    op.quantity, op.price, str(op.fx_rate) if op.fx_rate is not None else None, op.fx_source,
                    op.order_id, op.target_id, json.dumps(op.payload, ensure_ascii=False) if op.payload else None,
                    op.comment, op.to_account_id,
                ),
            ).lastrowid

    def _describe_violations(self, after: Ledger, before: Ledger) -> list[str]:
        known = {(v.op_id, v.message) for v in before.violations}
        result = []
        for v in after.violations:
            if (v.op_id, v.message) in known:
                continue
            ref = "новая операция" if v.op_id == PENDING_ID else f"операция #{v.op_id} ({KIND_NAMES[self.op(v.op_id).kind]})"
            result.append(f"{ref}: {v.message}")
        return result

    def _current(self, op_id: int) -> Op:
        """Покупка/продажа/исполнение в действующем виде (с учётом изменений)."""
        op = self.op(op_id)
        if op.kind not in ("buy", "sell", "order_filled"):
            raise JournalError(f"изменять и удалять можно только покупки и продажи, а #{op_id} — {KIND_NAMES[op.kind]}")
        if op.cancelled:
            raise JournalError(f"операция #{op_id} отменена")
        for current in effective_ops(self.ops()):
            if current.id == op_id:
                return current
        raise JournalError(f"операция #{op_id} удалена")

    def _open_order(self, order_id: int) -> OrderState:
        order = self.ledger().orders.get(order_id)
        if order is None or order.status != "open":
            raise JournalError(f"ордер #{order_id} не найден среди выставленных")
        return order

    def _rate(self, currency: Currency, op_date: date, fx_rate: Decimal | None) -> tuple[Decimal, str]:
        if fx_rate is not None:
            if Decimal(fx_rate) <= 0:
                raise JournalError("курс должен быть больше нуля")
            return Decimal(fx_rate), "manual"
        return self.fx.rate(currency, op_date)

    def _check_trade(self, item: str | None, account_id: int | None, currency: Currency | None,
                     quantity: int | None, price: int | None, op_date: date) -> None:
        if not item:
            raise JournalError("не указан предмет")
        if currency not in USER_CURRENCIES:
            raise JournalError(f"валюта должна быть одной из: {', '.join(c.name for c in USER_CURRENCIES)}")
        self._check_account(account_id)
        if op_date > today_msk():
            raise JournalError("дата операции в будущем")
        self._check_amounts(quantity, price)

    def _check_account(self, account_id: int | None) -> None:
        account = self.conn.execute(
            "SELECT 1 FROM accounts WHERE id = ? AND portfolio_id = ?", (account_id, self.portfolio_id)
        ).fetchone()
        if account is None:
            raise JournalError(f"аккаунт #{account_id} не найден")

    @staticmethod
    def _check_amounts(quantity: int | None, price: int | None) -> None:
        if not isinstance(quantity, int) or quantity <= 0:
            raise JournalError("количество должно быть целым больше нуля")
        if not isinstance(price, int) or price < 0:
            raise JournalError("цена не может быть отрицательной")


def _diff(before: Ledger, after: Ledger, violations: list[str]) -> Preview:
    positions = []
    for key in sorted(set(before.positions) | set(after.positions)):
        b, a = before.positions.get(key), after.positions.get(key)
        qb, qa = (b.quantity if b else 0), (a.quantity if a else 0)
        avg_b, avg_a = (b.avg_price if b else None), (a.avg_price if a else None)
        if qb != qa or avg_b != avg_a:
            positions.append(PositionChange(key, qb, qa, avg_b, avg_a))
    sales_b = {s.op_id: s for s in before.sales}
    sales_a = {s.op_id: s for s in after.sales}
    sales = []
    for op_id in sorted(set(sales_b) | set(sales_a)):
        b, a = sales_b.get(op_id), sales_a.get(op_id)
        if b is None or a is None or b.profit != a.profit or b.profit_usd != a.profit_usd:
            sales.append(SaleChange(op_id, b and b.profit, a and a.profit, b and b.profit_usd, a and a.profit_usd))
    return Preview(positions, sales, violations)


def create_account(conn: sqlite3.Connection, name: str, portfolio_id: int = 1) -> int:
    name = name.strip()
    if not name:
        raise JournalError("пустое название аккаунта")
    try:
        with conn:
            return conn.execute(
                "INSERT INTO accounts (portfolio_id, name, created_at) VALUES (?, ?, ?)", (portfolio_id, name, utcnow())
            ).lastrowid
    except sqlite3.IntegrityError as e:
        raise JournalError(f"аккаунт «{name}» уже есть") from e
