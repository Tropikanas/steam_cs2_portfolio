"""Демонстрация этапа 2 на отдельной временной базе: рабочую базу не меняет (только читает курсы Steam)."""

import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from steamfolio import db
from steamfolio.clock import today_msk
from steamfolio.currencies import Currency, format_minor
from steamfolio.fx import FxResolver
from steamfolio.journal import KIND_NAMES, Journal, JournalError, Preview, create_account
from steamfolio.ledger import Ledger

RUB = Currency.RUB
CASE = "Kilowatt Case"
AK = "AK-47 | Redline (Field-Tested)"


def usd(amount: Decimal) -> str:
    return "$" + format_minor(amount)


def show(title: str, ledger: Ledger, accounts: dict[int, str]) -> None:
    print(f"\n=== {title} ===")
    print(f"  {'предмет':<32} {'аккаунт':<9} {'вал':<4} {'кол':>4} {'средняя':>10} {'средняя $':>10} {'с даты':>11}")
    for p in ledger.open_positions:
        print(
            f"  {p.key.market_hash_name[:32]:<32} {accounts[p.key.account_id]:<9} {p.key.currency.name:<4} "
            f"{p.quantity:>4} {format_minor(p.avg_price):>10} {format_minor(p.avg_price_usd):>10} "
            f"{p.first_buy_date:%d.%m.%Y}"
        )
    for o in ledger.open_orders:
        print(f"  ордер #{o.order_id}: {o.market_hash_name}, {o.quantity} шт. по {format_minor(o.price)} {o.currency.name}")
    for s in ledger.sales:
        print(
            f"  продажа #{s.op_id}: {s.key.market_hash_name} {s.quantity} шт. по {format_minor(s.price)}, "
            f"прибыль {format_minor(s.profit)} {s.key.currency.name} = {usd(s.profit_usd)} ({s.profit_pct:.1f}%), "
            f"срок {s.months} мес."
        )
    print(f"  Всего вложено {usd(ledger.invested_usd)} · продано {usd(ledger.sold_usd)} · прибыль {usd(ledger.profit_usd)}")


def show_preview(preview: Preview) -> None:
    for c in preview.positions:
        print(
            f"    {c.key.market_hash_name}: количество {c.quantity_before} → {c.quantity_after}, "
            f"средняя {format_minor(c.avg_before)} → {format_minor(c.avg_after)}"
        )
    for s in preview.sales:
        print(f"    продажа #{s.op_id}: прибыль {format_minor(s.profit_before)} → {format_minor(s.profit_after)}")
    for v in preview.violations:
        print(f"    НЕЛЬЗЯ: {v}")


def run(main_db: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "demo.db")
        if main_db.exists():  # курсы Steam из рабочей базы, чтобы покупки «сегодня» шли по ним
            src = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
            snapshots = src.execute("SELECT run_id, taken_at, currency, rate, samples, spread_pct FROM fx_snapshots").fetchall()
            with conn:
                for run_id in {s[0] for s in snapshots}:
                    conn.execute("INSERT INTO collector_runs (id, started_at, status) VALUES (?, '', 'ok')", (run_id,))
                conn.executemany("INSERT INTO fx_snapshots VALUES (?, ?, ?, ?, ?, ?)", snapshots)
            src.close()
        j = Journal(conn, FxResolver(conn))
        accounts = {create_account(conn, "Основной"): "Основной", create_account(conn, "Твинк"): "Твинк"}
        main, twink = accounts

        old = j.buy(CASE, main, RUB, 100, 900, date(2021, 11, 15), comment="старая покупка")
        new = j.buy(CASE, main, RUB, 50, 1200)
        for op_id in (old, new):
            op = j.op(op_id)
            print(f"Покупка #{op_id} от {op.op_date:%d.%m.%Y}: курс {op.fx_rate:.4f} ₽/$ (источник: {op.fx_source})")
        sale = j.sell(CASE, main, RUB, 60, 1500)
        order = j.place_order(AK, twink, RUB, 2, 240000)
        j.fill_order(order, quantity=1)
        show("Портфель после покупок, продажи и частичного исполнения ордера", j.ledger(), accounts)

        print(f"\nПопытка откатить покупку #{old} (100 шт.), хотя из позиции уже продано 60:")
        show_preview(j.preview_cancel(old))
        try:
            j.cancel(old)
        except JournalError as e:
            print(f"  → отклонено: {str(e).splitlines()[0]}")

        print(f"\nОткат покупки #{new} (50 шт. по 12 ₽) — что изменится:")
        show_preview(j.preview_cancel(new))
        j.cancel(new)
        show(f"Портфель после отката #{new}", j.ledger(), accounts)

        print(f"\nИзменение продажи #{sale}: цена 15 ₽ → 20 ₽")
        j.edit(sale, price=2000)
        j.restore(new)
        show(f"Портфель после изменения продажи и возврата #{new}", j.ledger(), accounts)

        print("\nЖурнал:")
        for op in j.ops():
            mark = " (отменена)" if op.cancelled else ""
            print(f"  #{op.id:<3} {op.op_date:%d.%m.%Y}  {KIND_NAMES[op.kind]:<16} {op.market_hash_name or '':<32} "
                  f"{op.quantity or '':>4} × {format_minor(op.price)}{mark}")
        conn.close()
    print(f"\nДемо-база удалена; рабочая не менялась. Сегодня по Москве: {today_msk():%d.%m.%Y}")
    return 0
