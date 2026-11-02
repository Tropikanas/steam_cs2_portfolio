"""Записи портфеля: покупки, продажи, переносы и выставленные ордера.

Записи правятся и удаляются на месте — истории правок и отката нет, ошибку исправляют вручную.
Любое изменение сначала проверяется полным пересчётом в памяти: если он находит нарушения
(продажа больше остатка и т. п.), изменение отклоняется с перечнем записей, которые мешают.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from steamfolio.clock import today_msk
from steamfolio.currencies import USER_CURRENCIES, Currency
from steamfolio.db import utcnow
from steamfolio.fx import FxResolver
from steamfolio.ledger import (
    EDITABLE_FIELDS, Ledger, Op, RowKey, is_transfer_buy, removal_matches, replay,
)

# Временный id ещё не записанной операции: больше любого настоящего.
PENDING_ID = 10**15

KIND_NAMES = {
    "buy": "покупка",
    "sell": "продажа",
    "transfer": "перенос на аккаунт",
    "position_removed": "позиция удалена",
}


class JournalError(Exception):
    """Действие отклонено. violations — что мешает, по одной строке на запись."""

    def __init__(self, message: str, violations: list[str] | None = None):
        super().__init__(message if not violations else message + ":\n" + "\n".join(violations))
        self.violations = violations or []


@dataclass(frozen=True)
class Order:
    """Выставленный ордер на покупку (исполненные и убранные не хранятся)."""

    order_id: int
    market_hash_name: str
    account_id: int
    currency: Currency
    price: int
    quantity: int  # сколько ещё не куплено
    comment: str
    created_at: str

    @property
    def total(self) -> int:
        return self.price * self.quantity

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Order:
        return cls(row["id"], row["market_hash_name"], row["account_id"], Currency(row["currency"]), row["price"],
                   row["quantity"], row["comment"], row["created_at"])


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
            raise JournalError(f"запись #{op_id} не найдена")
        return Op.from_row(row)

    def orders(self) -> list[Order]:
        rows = self.conn.execute("SELECT * FROM orders WHERE portfolio_id = ? ORDER BY id", (self.portfolio_id,))
        return [Order.from_row(r) for r in rows]

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
        """Перенести часть строки на другой аккаунт: списание с исходного аккаунта по средней цене и обычная
        покупка на новом — по той же средней цене, датой первой покупки исходной строки и с тем же курсом к USD.
        Новую строку можно править как любую купленную; удаление этой покупки отменяет перенос целиком."""
        op_date = op_date or today_msk()
        self._check_trade(item, from_account_id, currency, quantity, 0, op_date)
        self._check_account(to_account_id)
        if to_account_id == from_account_id:
            raise JournalError("аккаунты «откуда» и «куда» совпадают")
        src = self.ledger().positions.get(RowKey(item, from_account_id, currency))
        if src is None or src.quantity < quantity:
            raise JournalError(f"перенос {quantity} шт. «{item}» при остатке {src.quantity if src else 0} шт.")
        now = utcnow()
        out = Op(PENDING_ID, "transfer", op_date, now, item, from_account_id, currency, quantity,
                 comment=comment, to_account_id=to_account_id, payload={"split": True})
        price = int(src.avg_price.to_integral_value(ROUND_HALF_UP))
        if src.avg_price_usd:
            rate, source = src.avg_price / src.avg_price_usd, "transfer"
        else:  # бесплатные предметы — курс на дату первой покупки
            rate, source = self._rate(currency, src.first_buy_date, None)
        arrival = Op(PENDING_ID + 1, "buy", src.first_buy_date, now, item, to_account_id, currency, quantity, price,
                     rate, source, target_id=PENDING_ID, comment=comment)
        return self._add_pair(out, arrival)

    def remove_position(self, item: str, account_id: int, currency: Currency) -> int:
        """Стереть весь текущий остаток строки как ошибочно внесённый: будто его не покупали.

        Снимаются последние пришедшие в строку штуки — покупки уменьшаются или удаляются; пришедшие
        переносом снимаются и с исходного аккаунта. Возвращает, сколько штук стёрто."""
        pos = self.ledger().positions.get(RowKey(item, account_id, currency))
        if pos is None or pos.quantity == 0:
            raise JournalError(f"позиции «{item}» в {currency.name} на этом аккаунте нет")
        ops = self.ops()
        request = Op(PENDING_ID, "position_removed", today_msk(), utcnow(), item, account_id, currency, pos.quantity)
        _, taken, matched = removal_matches(ops + [request])
        if PENDING_ID not in matched:
            raise JournalError(f"не удалось найти покупки, из которых сложилась строка «{item}»")
        after = [replace(op, quantity=op.quantity - taken.get(op.id, 0)) for op in ops
                 if taken.get(op.id, 0) < op.quantity]
        self._check(ops, after, "позиция не удалена")
        by_id = {op.id: op for op in ops}
        with self.conn:
            # Покупки — раньше переносов: привезённая покупка ссылается на свой перенос.
            for op_id, count in sorted(taken.items(), key=lambda kv: by_id[kv[0]].kind != "buy"):
                quantity = by_id[op_id].quantity
                if count >= quantity:
                    self.conn.execute("DELETE FROM operations WHERE id = ?", (op_id,))
                else:
                    self.conn.execute("UPDATE operations SET quantity = ? WHERE id = ?", (quantity - count, op_id))
        return pos.quantity

    def edit(self, op_id: int, **changes) -> int:
        """Изменить покупку или продажу на месте."""
        target = self._record(op_id)
        unknown = set(changes) - set(EDITABLE_FIELDS)
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
        if target.fields() == after.fields():
            raise JournalError("ничего не изменилось")
        ops = self.ops()
        self._check(ops, [after if op.id == op_id else op for op in ops], f"{KIND_NAMES[target.kind]} не изменена")
        with self.conn:
            self.conn.execute(
                """UPDATE operations SET op_date = ?, market_hash_name = ?, account_id = ?, currency = ?, quantity = ?,
                       price = ?, fx_rate = ?, fx_source = ?, comment = ? WHERE id = ?""",
                (after.op_date.isoformat(), after.market_hash_name, after.account_id, int(after.currency),
                 after.quantity, after.price, str(after.fx_rate) if after.fx_rate is not None else None,
                 after.fx_source, after.comment, op_id),
            )
        return op_id

    def delete(self, op_id: int) -> int:
        """Удалить покупку или продажу. Покупка, привезённая переносом, удаляется вместе с переносом:
        штуки возвращаются на исходный аккаунт."""
        target = self._record(op_id)
        gone = {op_id} | ({target.target_id} if is_transfer_buy(target) else set())
        ops = self.ops()
        self._check(ops, [op for op in ops if op.id not in gone], f"{KIND_NAMES[target.kind]} не удалена")
        with self.conn:
            # Сначала покупка: она ссылается на перенос.
            self.conn.execute("DELETE FROM operations WHERE id = ?", (op_id,))
            self.conn.executemany("DELETE FROM operations WHERE id = ?", [(i,) for i in gone - {op_id}])
        return op_id

    # ---------- ордера ----------

    def place_order(self, item: str, account_id: int, currency: Currency, quantity: int, price: int,
                    comment: str = "") -> int:
        self._check_trade(item, account_id, currency, quantity, price, today_msk())
        with self.conn:
            return self.conn.execute(
                """INSERT INTO orders (portfolio_id, market_hash_name, account_id, currency, quantity, price, comment,
                       created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.portfolio_id, item, account_id, int(currency), quantity, price, comment, utcnow()),
            ).lastrowid

    def change_order(self, order_id: int, price: int, quantity: int, comment: str | None = None,
                     account_id: int | None = None, currency: Currency | None = None) -> int:
        """Перевыставить ордер: цена, количество, комментарий, а также аккаунт и валюта (None — без изменений)."""
        order = self._order(order_id)
        account_id = order.account_id if account_id is None else account_id
        currency = order.currency if currency is None else Currency(currency)
        self._check_trade(order.market_hash_name, account_id, currency, quantity, price, today_msk())
        with self.conn:
            self.conn.execute(
                "UPDATE orders SET price = ?, quantity = ?, comment = ?, account_id = ?, currency = ? WHERE id = ?",
                (price, quantity, order.comment if comment is None else comment, account_id, int(currency), order_id),
            )
        return order_id

    def fill_order(self, order_id: int, quantity: int | None = None, op_date: date | None = None,
                   price: int | None = None, fx_rate: Decimal | None = None) -> int:
        """Ордер куплен (весь — quantity=None — или частично): покупка в портфеле, ордер уменьшается или уходит.
        Комментарий ордера дописывается в комментарий строки портфеля через «; »."""
        order = self._order(order_id)
        quantity = order.quantity if quantity is None else quantity
        if quantity > order.quantity:
            raise JournalError(f"куплено {quantity} шт. — больше, чем в ордере ({order.quantity} шт.)")
        op_id = self.buy(order.market_hash_name, order.account_id, order.currency, quantity,
                         order.price if price is None else price, op_date, fx_rate=fx_rate)
        with self.conn:
            if quantity == order.quantity:
                self.conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
            else:
                self.conn.execute("UPDATE orders SET quantity = ? WHERE id = ?", (order.quantity - quantity, order_id))
            if order.comment.strip():
                self._append_row_comment(order.market_hash_name, order.account_id, order.currency, order.comment.strip())
        return op_id

    def remove_order(self, order_id: int) -> int:
        self._order(order_id)
        with self.conn:
            self.conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        return order_id

    def _append_row_comment(self, item: str, account_id: int, currency: Currency, text: str) -> None:
        """Дописать в комментарий строки портфеля; тот же текст второй раз не повторяется."""
        row = self.conn.execute(
            """SELECT comment FROM position_comments
               WHERE portfolio_id = ? AND market_hash_name = ? AND account_id = ? AND currency = ?""",
            (self.portfolio_id, item, account_id, int(currency)),
        ).fetchone()
        parts = [p.strip() for p in (row["comment"] if row else "").split(";") if p.strip()]
        if text in parts:
            return
        self.conn.execute(
            """INSERT INTO position_comments (portfolio_id, market_hash_name, account_id, currency, comment)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET comment = excluded.comment""",
            (self.portfolio_id, item, account_id, int(currency), "; ".join([*parts, text])[:500]),
        )

    # ---------- внутреннее ----------

    def _check(self, before_ops: list[Op], after_ops: list[Op], action: str) -> None:
        problems = self._describe_violations(replay(after_ops), replay(before_ops))
        if problems:
            raise JournalError(f"{action}: противоречит другим записям", problems)

    def _add(self, op: Op) -> int:
        ops = self.ops()
        self._check(ops, ops + [op], f"{KIND_NAMES[op.kind]} не записана")
        with self.conn:
            return self._insert(op)

    def _add_pair(self, first: Op, second: Op) -> int:
        """Две связанные записи разом (second.target_id указывает на first); возвращает id первой."""
        ops = self.ops()
        self._check(ops, ops + [first, second], f"{KIND_NAMES[first.kind]} не записан")
        with self.conn:
            first_id = self._insert(first)
            self._insert(replace(second, target_id=first_id))
        return first_id

    def _insert(self, op: Op) -> int:
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
            ref = "новая запись" if v.op_id >= PENDING_ID else f"запись #{v.op_id} ({KIND_NAMES[self.op(v.op_id).kind]})"
            result.append(f"{ref}: {v.message}")
        return result

    def _record(self, op_id: int) -> Op:
        """Покупка или продажа, которую можно изменить или удалить."""
        op = self.op(op_id)
        if op.kind not in ("buy", "sell"):
            raise JournalError(f"изменять и удалять можно только покупки и продажи, а #{op_id} — {KIND_NAMES[op.kind]}")
        return op

    def _order(self, order_id: int) -> Order:
        row = self.conn.execute(
            "SELECT * FROM orders WHERE id = ? AND portfolio_id = ?", (order_id, self.portfolio_id)
        ).fetchone()
        if row is None:
            raise JournalError(f"ордер #{order_id} не найден среди выставленных")
        return Order.from_row(row)

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
            raise JournalError("валюта должна быть одной из валют Steam")
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
