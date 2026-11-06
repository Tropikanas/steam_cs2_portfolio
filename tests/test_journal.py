from datetime import date
from decimal import Decimal

import pytest

from steamfolio.currencies import Currency
from steamfolio.journal import JournalError

RUB, USD, CNY = Currency.RUB, Currency.USD, Currency.CNY
CASE = "Kilowatt Case"
D = date.fromisoformat


def pos(journal, acc, currency=RUB, item=CASE):
    """Все строки (покупки) предмета в валюте на аккаунте вместе."""
    return journal.ledger().total(item, acc, currency)


# ---------- покупки, продажи, строки ----------

def test_tz_example_buy_1000_sell_2000(journal, acc):
    """ТЗ: купил за 1000 ₽, продал за 2000 ₽ — позиции нет, +1000 вложено, +2000 продано, +1000 прибыли."""
    journal.buy(CASE, acc, RUB, 1, 100000, D("2026-01-10"))
    journal.sell(CASE, acc, RUB, 1, 200000, D("2026-02-10"))
    ledger = journal.ledger()
    assert ledger.open_positions == []
    assert len(ledger.closed_positions) == 1
    sale = ledger.sales[0]
    assert sale.profit == 100000
    assert sale.profit_pct == 100
    # по курсу 80: вложено $12.50, продано $25.00, прибыль $12.50
    assert ledger.invested_usd == 1250
    assert ledger.sold_usd == 2500
    assert ledger.profit_usd == 1250


def test_each_purchase_is_its_own_row_and_sale_uses_its_price(journal, acc):
    first = journal.buy(CASE, acc, RUB, 10, 10000, D("2026-01-10"))
    second = journal.buy(CASE, acc, RUB, 10, 20000, D("2026-01-11"))
    ledger = journal.ledger()
    assert sorted((p.key.lot, p.quantity, p.avg_price) for p in ledger.open_positions) == \
        [(first, 10, 10000), (second, 10, 20000)]  # строки не складываются
    journal.sell(second, 5, 30000, D("2026-01-12"))  # продажа — из конкретной строки
    ledger = journal.ledger()
    assert ledger.sales[0].profit == (30000 - 20000) * 5
    assert ledger.lot(second).quantity == 5 and ledger.lot(first).quantity == 10
    assert pos(journal, acc).quantity == 15


def test_profit_in_usd_uses_rates_of_purchase_and_sale_dates(journal, fx, acc):
    fx.by_date[(RUB, D("2026-01-10"))] = Decimal(85)
    fx.by_date[(RUB, D("2026-03-01"))] = Decimal(100)
    lot = journal.buy(CASE, acc, RUB, 2, 850000, D("2026-01-10"))  # 17 000 ₽ = $200
    journal.sell(lot, 2, 1000000, D("2026-03-01"))  # выручка 20 000 ₽ = $200
    sale = journal.ledger().sales[0]
    assert sale.profit == 2 * (1000000 - 850000)  # 3000 ₽ в рублях
    assert sale.profit_usd == 0  # а в долларах ноль: рубль подешевел


def test_reopened_position_resets_average_and_first_date(journal, acc):
    journal.buy(CASE, acc, RUB, 2, 10000, D("2026-01-01"))
    journal.sell(CASE, acc, RUB, 2, 15000, D("2026-02-01"))
    journal.buy(CASE, acc, RUB, 1, 50000, D("2026-03-01"))
    p = pos(journal, acc)
    assert p.quantity == 1
    assert p.avg_price == 50000
    assert p.first_buy_date == D("2026-03-01")


def test_rows_are_split_by_account_and_currency(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 1, 1000, D("2026-01-01"))
    journal.buy(CASE, acc2, RUB, 2, 2000, D("2026-01-01"))
    journal.buy(CASE, acc, CNY, 3, 100, D("2026-01-01"))
    keys = {p.key[:3]: p.quantity for p in journal.ledger().open_positions}
    assert keys == {(CASE, acc, RUB): 1, (CASE, acc2, RUB): 2, (CASE, acc, CNY): 3}


def test_months_held(journal, acc):
    journal.buy(CASE, acc, RUB, 1, 1000, D("2026-01-01"))
    journal.sell(CASE, acc, RUB, 1, 2000, D("2026-04-01"))  # 90 дней
    assert journal.ledger().sales[0].months == Decimal("3.0")


def test_cannot_sell_more_than_owned(journal, acc):
    lot = journal.buy(CASE, acc, RUB, 1, 1000, D("2026-01-01"))
    with pytest.raises(JournalError, match="при остатке 1"):
        journal.sell(lot, 2, 1000, D("2026-01-02"))
    with pytest.raises(JournalError, match="строки"):  # продажа раньше покупки — строки тогда ещё не было
        journal.sell(lot, 1, 1000, D("2025-12-31"))


def test_backdated_purchase_is_a_separate_row(journal, acc):
    lot = journal.buy(CASE, acc, RUB, 1, 10000, D("2026-01-10"))
    journal.sell(lot, 1, 20000, D("2026-02-01"))
    journal.buy(CASE, acc, RUB, 1, 30000, D("2026-01-15"))  # внесена позже, датирована до продажи — своя строка
    ledger = journal.ledger()
    assert (ledger.sales[0].avg_price, ledger.sales[0].profit) == (10000, 10000)  # прошлая продажа не меняется
    assert pos(journal, acc).quantity == 1


def test_validation(journal, acc):
    with pytest.raises(JournalError, match="валют Steam"):
        journal.buy(CASE, acc, None, 1, 100, D("2026-01-01"))
    with pytest.raises(JournalError):
        journal.buy(CASE, acc, RUB, 0, 100, D("2026-01-01"))
    with pytest.raises(JournalError):
        journal.buy(CASE, 999, RUB, 1, 100, D("2026-01-01"))
    with pytest.raises(JournalError, match="будущем"):
        journal.buy(CASE, acc, RUB, 1, 100, D("2099-01-01"))


def test_manual_rate_is_kept(journal, acc):
    op_id = journal.buy(CASE, acc, RUB, 1, 9000, D("2026-01-01"), fx_rate=Decimal("90"))
    op = journal.op(op_id)
    assert (op.fx_rate, op.fx_source) == (Decimal(90), "manual")
    assert pos(journal, acc).cost_usd == 100


# ---------- перенос между аккаунтами ----------

def test_transfer_moves_quantity_with_average_and_first_date(journal, fx, acc, acc2):
    fx.by_date[(RUB, D("2026-01-10"))] = Decimal(100)
    journal.buy(CASE, acc, RUB, 100, 1000, D("2026-01-10"))  # 10 ₽ = $0.10 за штуку
    journal.transfer(CASE, acc, acc2, RUB, 30, D("2026-02-01"))
    src, dst = pos(journal, acc), pos(journal, acc2)
    assert (src.quantity, src.avg_price, src.avg_price_usd) == (70, 1000, 10)
    assert (dst.quantity, dst.avg_price, dst.avg_price_usd) == (30, 1000, 10)
    assert dst.first_buy_date == D("2026-01-10")
    ledger = journal.ledger()
    assert ledger.invested_usd == 1000  # перенос не покупка
    assert ledger.sales == []


def test_transfer_into_existing_row_averages(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 10, 1000, D("2026-01-10"))
    journal.buy(CASE, acc2, RUB, 10, 2000, D("2026-01-05"))
    journal.transfer(CASE, acc, acc2, RUB, 10, D("2026-01-20"))
    dst = pos(journal, acc2)
    assert (dst.quantity, dst.avg_price, dst.first_buy_date) == (20, 1500, D("2026-01-05"))
    assert pos(journal, acc).quantity == 0


def test_sale_after_transfer_uses_carried_cost(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 10, 1000, D("2026-01-01"))
    journal.transfer(CASE, acc, acc2, RUB, 4, D("2026-01-02"))
    journal.sell(CASE, acc2, RUB, 4, 1500, D("2026-04-01"))
    sale = journal.ledger().sales[0]
    assert sale.profit == 4 * 500
    assert sale.months == Decimal("3.0")  # срок — от первой покупки на исходном аккаунте


def _arrival(journal, transfer_id):
    return next(op for op in journal.ops() if op.kind == "buy" and op.target_id == transfer_id)


def test_transfer_validation(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 5, 1000, D("2026-01-01"))
    with pytest.raises(JournalError, match="при остатке 5"):
        journal.transfer(CASE, acc, acc2, RUB, 6, D("2026-01-02"))
    with pytest.raises(JournalError, match="совпадают"):
        journal.transfer(CASE, acc, acc, RUB, 1, D("2026-01-02"))
    with pytest.raises(JournalError):
        journal.transfer(CASE, acc, 999, RUB, 1, D("2026-01-02"))
    moved = journal.transfer(CASE, acc, acc2, RUB, 5, D("2026-01-02"))
    with pytest.raises(JournalError, match="только покупки и продажи"):
        journal.edit(moved, quantity=1)


def test_transferred_row_is_an_editable_buy(journal, fx, acc, acc2):
    fx.by_date[(RUB, D("2026-01-10"))] = Decimal(100)
    journal.buy(CASE, acc, RUB, 100, 1000, D("2026-01-10"))
    moved = journal.transfer(CASE, acc, acc2, RUB, 30, comment="на второй")
    buy = _arrival(journal, moved)
    # покупка на новом аккаунте: цена, дата покупки и курс исходной строки
    assert (buy.account_id, buy.quantity, buy.price, buy.op_date, buy.fx_rate, buy.comment) == \
        (acc2, 30, 1000, D("2026-01-10"), Decimal(100), "на второй")
    assert journal.ledger().invested_usd == 1000  # перенос не покупка: «Всего вложено» то же
    # правится как любая покупка — исходная строка не меняется
    journal.edit(buy.id, price=1200, currency=USD)
    assert pos(journal, acc2, USD).quantity == 30 and pos(journal, acc2).quantity == 0
    assert pos(journal, acc).quantity == 70


def test_deleting_transferred_buy_returns_items(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 10, 1000, D("2026-01-10"))
    moved = journal.transfer(CASE, acc, acc2, RUB, 4)
    sale = journal.sell(CASE, acc2, RUB, 2, 1500)
    with pytest.raises(JournalError, match=f"#{sale}"):  # привезённое уже продано
        journal.delete(_arrival(journal, moved).id)
    journal.delete(sale)
    journal.delete(_arrival(journal, moved).id)
    assert (pos(journal, acc).quantity, pos(journal, acc2).quantity) == (10, 0)
    assert [op.kind for op in journal.ops()] == ["buy"]  # перенос удалён вместе с покупкой


def test_transfer_does_not_mix_with_earlier_history_of_target_row(journal, acc, acc2):
    # На acc2 раньше уже была своя строка: куплена и продана до переноса. Привезённая покупка датирована
    # первой покупкой исходной строки (01.01), но в прошлую продажу не вмешивается.
    journal.buy(CASE, acc, RUB, 10, 2000, D("2026-01-01"))
    journal.buy(CASE, acc2, RUB, 10, 1000, D("2026-01-05"))
    journal.sell(CASE, acc2, RUB, 10, 1500, D("2026-01-06"))
    journal.transfer(CASE, acc, acc2, RUB, 10)
    ledger = journal.ledger()
    assert ledger.sales[0].profit == 10 * 500
    dst = pos(journal, acc2)
    assert (dst.quantity, dst.avg_price, dst.first_buy_date) == (10, 2000, D("2026-01-01"))


def test_transfer_not_counted_twice_on_chart(journal, acc, acc2):
    from steamfolio.ledger import daily_holdings

    journal.buy(CASE, acc, RUB, 100, 1000, D("2026-01-10"))
    journal.transfer(CASE, acc, acc2, RUB, 30)  # сегодня; покупка на acc2 датирована 10.01
    by_day = {day: {k.account_id: v[0] for k, v in s.items()} for day, s in daily_holdings(journal.ops())}
    assert by_day[D("2026-01-10")] == {acc: 100}
    assert list(by_day.values())[-1] == {acc: 70, acc2: 30}


# ---------- ордера ----------

def order_state(journal, order_id):
    return next((o for o in journal.orders() if o.order_id == order_id), None)


def test_order_partial_then_full_fill(journal, acc):
    order = journal.place_order(CASE, acc, RUB, 10, 5000)
    journal.fill_order(order, quantity=4)
    assert order_state(journal, order).quantity == 6
    assert pos(journal, acc).quantity == 4
    journal.fill_order(order, price=4900)  # «Куплено всё», фактическая цена отличалась
    assert journal.orders() == []  # купленный ордер не хранится
    p = pos(journal, acc)
    assert p.quantity == 10
    assert p.avg_price == Decimal(4 * 5000 + 6 * 4900) / 10


def test_order_fill_cannot_exceed_remaining(journal, acc):
    order = journal.place_order(CASE, acc, RUB, 2, 5000)
    with pytest.raises(JournalError, match="больше, чем в ордере"):
        journal.fill_order(order, quantity=3)
    assert order_state(journal, order).quantity == 2 and journal.ops() == []


def test_order_change_account_and_currency(journal, acc, acc2):
    # Перевыставление на другой аккаунт и в другой валюте: покупка идёт уже туда.
    order = journal.place_order(CASE, acc, RUB, 10, 5000)
    journal.change_order(order, price=70, quantity=10, account_id=acc2, currency=USD)
    o = order_state(journal, order)
    assert (o.account_id, o.currency, o.price) == (acc2, USD, 70)
    journal.fill_order(order)
    assert pos(journal, acc2, USD).quantity == 10 and pos(journal, acc).quantity == 0


def test_order_change_and_remove(journal, acc):
    order = journal.place_order(CASE, acc, RUB, 10, 5000, comment="старт")
    journal.change_order(order, price=5500, quantity=8, comment="перебили")
    o = order_state(journal, order)
    assert (o.price, o.quantity, o.comment) == (5500, 8, "перебили")
    journal.remove_order(order)
    assert journal.orders() == []
    with pytest.raises(JournalError, match="не найден"):
        journal.fill_order(order)


def test_orders_are_not_in_statistics(journal, acc):
    journal.place_order(CASE, acc, RUB, 10, 5000)
    ledger = journal.ledger()
    assert ledger.invested_usd == 0
    assert ledger.positions == {}


def test_editing_fill_does_not_touch_order(journal, acc, acc2):
    order = journal.place_order(CASE, acc, RUB, 10, 5000)
    fill = journal.fill_order(order, quantity=4)
    journal.edit(fill, account_id=acc2, currency=USD, quantity=7, price=60)
    assert order_state(journal, order).quantity == 6  # ордер как был
    assert pos(journal, acc2, USD).quantity == 7 and pos(journal, acc).quantity == 0


def test_each_order_fill_is_its_own_row_with_order_comment(journal, acc):
    # Каждая покупка по ордеру — отдельная строка с комментарием ордера.
    first = journal.place_order(CASE, acc, RUB, 10, 5000, comment="под мажор")
    a = journal.fill_order(first, quantity=4)
    b = journal.fill_order(first)
    second = journal.place_order(CASE, acc, RUB, 5, 4000, comment="докупка на просадке")
    c = journal.fill_order(second, quantity=5)
    quiet = journal.fill_order(journal.place_order(CASE, acc, RUB, 1, 4000))
    rows = {p.key.lot: p.quantity for p in journal.ledger().open_positions}
    assert rows == {a: 4, b: 6, c: 5, quiet: 1}
    assert [journal.op(i).comment for i in (a, b, c, quiet)] == ["под мажор", "под мажор", "докупка на просадке", ""]


# ---------- изменение и удаление ----------

def test_edit_changes_record_in_place(journal, acc):
    buy = journal.buy(CASE, acc, RUB, 2, 10000, D("2026-01-01"))
    journal.edit(buy, price=12000)
    assert pos(journal, acc).avg_price == 12000
    assert [(op.id, op.price) for op in journal.ops()] == [(buy, 12000)]  # та же запись, без истории правок


def test_edit_date_refreshes_rate(journal, fx, acc):
    fx.by_date[(RUB, D("2026-01-05"))] = Decimal(100)
    buy = journal.buy(CASE, acc, RUB, 1, 10000, D("2026-01-01"))
    journal.edit(buy, op_date=D("2026-01-05"))
    assert pos(journal, acc).cost_usd == 100


def test_edit_that_breaks_later_sale_is_rejected(journal, acc):
    buy = journal.buy(CASE, acc, RUB, 5, 1000, D("2026-01-01"))
    sale = journal.sell(CASE, acc, RUB, 4, 2000, D("2026-01-02"))
    with pytest.raises(JournalError, match=f"#{sale}"):
        journal.edit(buy, quantity=3)
    assert journal.op(buy).quantity == 5


def test_delete_buy_blocked_by_sale(journal, acc):
    buy = journal.buy(CASE, acc, RUB, 5, 1000, D("2026-01-01"))
    sale = journal.sell(CASE, acc, RUB, 3, 2000, D("2026-01-05"))
    with pytest.raises(JournalError, match=f"#{sale}"):
        journal.delete(buy)
    journal.delete(sale)
    journal.delete(buy)
    assert journal.ops() == [] and pos(journal, acc).quantity == 0
    with pytest.raises(JournalError, match="не найдена"):
        journal.edit(buy, price=5)


def test_edit_sale(journal, acc):
    journal.buy(CASE, acc, RUB, 2, 1000, D("2026-01-01"))
    sale = journal.sell(CASE, acc, RUB, 2, 1500, D("2026-01-02"))
    journal.edit(sale, price=2000)
    assert journal.ledger().sales[0].profit == 2000


