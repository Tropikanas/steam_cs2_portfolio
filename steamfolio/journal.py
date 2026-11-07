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
from decimal import Decimal

from steamfolio.clock import today_msk
from steamfolio.currencies import USER_CURRENCIES, Currency
from steamfolio.db import utcnow
from steamfolio.fx import FxResolver
from steamfolio.ledger import EDITABLE_FIELDS, Ledger, Op, is_transfer_buy, lot_of, replay

# Временный id ещё не записанной операции: больше любого настоящего.
PENDING_ID = 10**15

KIND_NAMES = {
    "buy": "покупка",
    "sell": "продажа",
    "transfer": "перенос на аккаунт",
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
        self, lot_id: int, quantity: int, price: int,
        op_date: date | None = None, comment: str = "", fx_rate: Decimal | None = None,
    ) -> int:
        """Продажа из строки (покупки) lot_id. price — чистая цена за штуку после комиссии, в валюте строки."""
        lot = self._lot(lot_id)
        op_date = op_date or today_msk()
        self._check_trade(lot.market_hash_name, lot.account_id, lot.currency, quantity, price, op_date)
        rate, source = self._rate(lot.currency, op_date, fx_rate)
        return self._add(Op(PENDING_ID, "sell", op_date, utcnow(), lot.market_hash_name, lot.account_id, lot.currency,
                            quantity, price, rate, source, target_id=lot_id, comment=comment))

    def transfer(self, lot_id: int, to_account_id: int, quantity: int, comment: str = "",
                 op_date: date | None = None) -> int:
        """Перенести часть строки на другой аккаунт: списание из строки и новая строка на другом аккаунте —
        покупка по той же цене, с той же датой и курсом к USD. Её можно править как любую покупку;
        удаление этой покупки отменяет перенос целиком."""
        lot = self._lot(lot_id)
        op_date = op_date or today_msk()
        self._check_trade(lot.market_hash_name, lot.account_id, lot.currency, quantity, 0, op_date)
        self._check_account(to_account_id)
        if to_account_id == lot.account_id:
            raise JournalError("аккаунты «откуда» и «куда» совпадают")
        src = self.ledger().lot(lot_id)
        if src is None or src.quantity < quantity:
            raise JournalError(f"перенос {quantity} шт. «{lot.market_hash_name}» при остатке {src.quantity if src else 0} шт.")
        if quantity == src.bought:
            # Переносится вся позиция, из неё ничего не продавали и не переносили: та же строка с тем же ID
            # просто переезжает на другой аккаунт (комментарий — из формы).
            ops = self.ops()
            moved = replace(lot, account_id=to_account_id, comment=comment)
            self._check(ops, [moved if op.id == lot_id else op for op in ops], "перенос не записан")
            with self.conn:
                self.conn.execute("UPDATE operations SET account_id = ?, comment = ? WHERE id = ?",
                                  (to_account_id, comment, lot_id))
            return lot_id
        # Часть позиции или позиция с продажами (её ID уже в «Закрытых позициях») — новая строка с новым ID.
        now = utcnow()
        out = Op(PENDING_ID, "transfer", op_date, now, lot.market_hash_name, lot.account_id, lot.currency, quantity,
                 comment=comment, to_account_id=to_account_id, payload={"split": True, "lot": lot_id})
        arrival = Op(PENDING_ID + 1, "buy", lot.op_date, now, lot.market_hash_name, to_account_id, lot.currency,
                     quantity, lot.price, lot.fx_rate, lot.fx_source, target_id=PENDING_ID, comment=comment)
        return self._add_pair(out, arrival)

    def remove_position(self, lot_id: int) -> int:
        """Стереть остаток строки как ошибочно внесённый: покупка уменьшается на остаток (без продаж и
        переносов — удаляется), будто его не покупали. Если строка привезена переносом, на столько же
        уменьшается перенос и стирается остаток в исходной строке. Возвращает, сколько штук стёрто."""
        pos = self.ledger().lot(lot_id)
        if pos is None or pos.quantity == 0:
            raise JournalError("в этой строке ничего не осталось")
        count = pos.quantity
        ops = self.ops()
        by_id = {op.id: op for op in ops}
        cut: dict[int, int] = {}  # id записи → на сколько уменьшить
        lot = by_id[lot_id]
        while lot is not None:
            cut[lot.id] = count
            moved = by_id.get(lot.target_id) if is_transfer_buy(lot) else None
            if moved is None:
                break
            cut[moved.id] = count  # перенос меньше на столько же, штуки «возвращаются» в исходную строку…
            lot = by_id.get((moved.payload or {}).get("lot"))  # …и стираются там
        after = [replace(op, quantity=op.quantity - cut.get(op.id, 0)) for op in ops
                 if op.quantity > cut.get(op.id, 0)]
        self._check(ops, after, "позиция не удалена")
        with self.conn:
            # Покупки — раньше переносов: привезённая покупка ссылается на свой перенос.
            for op_id, n in sorted(cut.items(), key=lambda kv: by_id[kv[0]].kind != "buy"):
                if n >= by_id[op_id].quantity:
                    self.conn.execute("DELETE FROM operations WHERE id = ?", (op_id,))
                else:
                    self.conn.execute("UPDATE operations SET quantity = ? WHERE id = ?", (by_id[op_id].quantity - n, op_id))
        return count

    def edit(self, op_id: int, **changes) -> int:
        """Изменить покупку или продажу на месте. У продажи предмет, аккаунт и валюта — от её строки;
        если они меняются у покупки, продажи и переносы из этой строки меняются вместе с ней."""
        target = self._record(op_id)
        unknown = set(changes) - set(EDITABLE_FIELDS)
        if unknown:
            raise JournalError(f"нельзя изменить поля: {', '.join(sorted(unknown))}")
        if target.kind == "sell" and {"market_hash_name", "account_id", "currency"} & set(changes):
            raise JournalError("у продажи предмет, аккаунт и валюта — как у строки, из которой продано")
        moves = {k for k in ("market_hash_name", "account_id", "currency")
                 if k in changes and changes[k] != getattr(target, k) and not (k == "currency" and Currency(changes[k]) == target.currency)}
        if target.kind == "buy" and moves and any(op.kind == "sell" and op.target_id == op_id for op in self.ops()):
            # Иначе прошлые продажи в «Закрытых позициях» оказались бы с другого аккаунта (в другой валюте, другого предмета).
            raise JournalError(
                f"Аккаунт и валюту по позиции с ID {target.position_no} редактировать нельзя - данная позиция уже "
                "частично продана с прежними данными.\n"
                "Изменение кол-ва закупленного, цены и даты закупки разрешена - произойдёт пересчёт закрытых позиций.")
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
        same = {"market_hash_name": after.market_hash_name, "account_id": after.account_id, "currency": after.currency}
        followers = [replace(op, **same) for op in ops if op.kind in ("sell", "transfer") and lot_of(op) == op_id]
        changed = {op.id: op for op in [after, *followers]}
        new_ops = [changed.get(op.id, op) for op in ops]
        self._check_dates(replay(new_ops), replay(ops), new_ops, target.kind)
        before_violations = {(v.op_id, v.message) for v in replay(ops).violations}
        same_lot = {op.id for op in new_ops if lot_of(op) == target.target_id and op.kind in ("sell", "transfer")}
        if target.kind == "sell" and any(v.op_id in same_lot and (v.op_id, v.message) not in before_violations
                                         for v in replay(new_ops).violations):
            # Продажа больше, чем было в её строке, — называем позицию, как в «Текущем портфеле».
            # Купленное по позиции = её остаток в портфеле + всё проданное из неё (по «Закрытым позициям»).
            ledger = replay(ops)
            no = next((op.position_no for op in ops if op.id == target.target_id), None)
            lot = ledger.lot(target.target_id)
            bought = (lot.quantity if lot else 0) + sum(s.quantity for s in ledger.sales if s.key.lot == target.target_id)
            raise JournalError(
                f"Количество продаваемых предметов по позиции с ID {no} не может превышать количество купленного "
                f"по данной позиции ({bought}). См. «Текущий портфель», позиция ID {no}.")
        self._check(ops, new_ops, f"{KIND_NAMES[target.kind]} не изменена", target.kind)
        with self.conn:
            for op in changed.values():
                self.conn.execute(
                    """UPDATE operations SET op_date = ?, market_hash_name = ?, account_id = ?, currency = ?,
                           quantity = ?, price = ?, fx_rate = ?, fx_source = ?, comment = ? WHERE id = ?""",
                    (op.op_date.isoformat(), op.market_hash_name, op.account_id, int(op.currency), op.quantity,
                     op.price, str(op.fx_rate) if op.fx_rate is not None else None, op.fx_source, op.comment, op.id),
                )
        return op_id

    def delete(self, op_id: int) -> int:
        """Удалить покупку или продажу. Покупка, привезённая переносом, удаляется вместе с переносом:
        штуки возвращаются в исходную строку. Удалённая продажа возвращает штуки в свою строку."""
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
        """Ордер куплен (весь — quantity=None — или частично): новая строка в портфеле с комментарием ордера,
        ордер уменьшается или уходит. Каждая покупка по ордеру — своя строка."""
        order = self._order(order_id)
        quantity = order.quantity if quantity is None else quantity
        if quantity > order.quantity:
            raise JournalError(f"куплено {quantity} шт. — больше, чем в ордере ({order.quantity} шт.)")
        op_id = self.buy(order.market_hash_name, order.account_id, order.currency, quantity,
                         order.price if price is None else price, op_date, comment=order.comment.strip(),
                         fx_rate=fx_rate)
        with self.conn:
            if quantity == order.quantity:
                self.conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
            else:
                self.conn.execute("UPDATE orders SET quantity = ? WHERE id = ?", (order.quantity - quantity, order_id))
        return op_id

    def remove_order(self, order_id: int) -> int:
        self._order(order_id)
        with self.conn:
            self.conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        return order_id

    # ---------- внутреннее ----------

    def _check(self, before_ops: list[Op], after_ops: list[Op], action: str, edited: str = "sell") -> None:
        after, before = replay(after_ops), replay(before_ops)
        self._check_dates(after, before, after_ops, edited)
        problems = self._describe_violations(after, before)
        if problems:
            raise JournalError(f"{action}: противоречит другим записям", problems)

    @staticmethod
    def _check_dates(after: Ledger, before: Ledger, after_ops: list[Op], edited: str = "sell") -> None:
        """Продажа раньше покупки своей позиции — понятным текстом. edited — что меняли: дату продажи
        («Закрытые позиции» или новая продажа) или дату покупки («Текущий портфель»)."""
        known = {(v.op_id, v.message) for v in before.violations}
        by_id = {op.id: op for op in after_ops}
        for v in after.violations:
            sale = by_id.get(v.op_id)
            lot = by_id.get(sale.target_id) if sale and sale.kind == "sell" else None
            if (v.op_id, v.message) in known or lot is None or sale.op_date >= lot.op_date:
                continue
            if edited == "buy":
                raise JournalError(
                    f"Дата покупки ({lot.op_date:%d.%m.%Y}) не может быть позже даты продажи позиции с ID "
                    f"{lot.position_no} ({sale.op_date:%d.%m.%Y}). При необходимости, измените дату продажи "
                    "во вкладке «Закрытые позиции»")
            raise JournalError(
                f"Дата продажи ({sale.op_date:%d.%m.%Y}) не может быть раньше даты покупки позиции с ID "
                f"{lot.position_no} ({lot.op_date:%d.%m.%Y}). При необходимости, удалите продажу и измените "
                "дату покупки во вкладке «Текущий портфель»")

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
        if op.kind == "buy" and op.position_no is None:
            op = replace(op, position_no=self._free_position_no())
        return self.conn.execute(
            """INSERT INTO operations (portfolio_id, user_id, kind, op_date, created_at, market_hash_name,
                   account_id, currency, quantity, price, fx_rate, fx_source, order_id, target_id, payload, comment,
                   to_account_id, position_no)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.portfolio_id, self.user_id, op.kind, op.op_date.isoformat(), op.created_at,
                op.market_hash_name, op.account_id, int(op.currency) if op.currency is not None else None,
                op.quantity, op.price, str(op.fx_rate) if op.fx_rate is not None else None, op.fx_source,
                op.order_id, op.target_id, json.dumps(op.payload, ensure_ascii=False) if op.payload else None,
                op.comment, op.to_account_id, op.position_no,
            ),
        ).lastrowid

    def _free_position_no(self) -> int:
        """«ID позиции» новой строки: наименьший номер, которого нет ни в «Текущем портфеле» (строки с остатком),
        ни в «Закрытых позициях» (строки, из которых продавали). Стёртый корзиной номер из середины
        возвращается; если пропусков нет — на 1 больше наибольшего."""
        ops = self.ops()
        ledger = replay(ops)
        visible = {p.key.lot for p in ledger.open_positions} | {s.key.lot for s in ledger.sales}
        taken = {op.position_no for op in ops if op.id in visible and op.position_no}
        return next(n for n in range(1, len(taken) + 2) if n not in taken)

    def _describe_violations(self, after: Ledger, before: Ledger) -> list[str]:
        known = {(v.op_id, v.message) for v in before.violations}
        result = []
        for v in after.violations:
            if (v.op_id, v.message) in known:
                continue
            ref = "новая запись" if v.op_id >= PENDING_ID else f"запись #{v.op_id} ({KIND_NAMES[self.op(v.op_id).kind]})"
            result.append(f"{ref}: {v.message}")
        return result

    def _lot(self, lot_id: int) -> Op:
        """Строка портфеля — её покупка."""
        op = self.op(lot_id)
        if op.kind != "buy":
            raise JournalError(f"#{lot_id} — не строка портфеля")
        return op

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
            raise JournalError("Количество должно быть целым больше нуля")
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
