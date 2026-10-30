from datetime import date
from decimal import Decimal

import pytest

from steamfolio.currencies import Currency
from steamfolio.journal import JournalError
from steamfolio.ledger import Position, RowKey

RUB, USD, CNY = Currency.RUB, Currency.USD, Currency.CNY
CASE = "Kilowatt Case"
D = date.fromisoformat


def pos(journal, acc, currency=RUB, item=CASE):
    key = RowKey(item, acc, currency)
    return journal.ledger().positions.get(key) or Position(key)


# ---------- покупки, продажи, средняя цена ----------

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


def test_average_price_is_weighted_and_sale_does_not_change_it(journal, acc):
    journal.buy(CASE, acc, RUB, 10, 10000, D("2026-01-10"))
    journal.buy(CASE, acc, RUB, 10, 20000, D("2026-01-11"))
    assert pos(journal, acc).avg_price == 15000
    journal.sell(CASE, acc, RUB, 5, 30000, D("2026-01-12"))
    p = pos(journal, acc)
    assert p.quantity == 15
    assert p.avg_price == 15000
    assert journal.ledger().sales[0].profit == (30000 - 15000) * 5


def test_profit_in_usd_uses_rates_of_purchase_and_sale_dates(journal, fx, acc):
    fx.by_date[(RUB, D("2026-01-10"))] = Decimal(80)
    fx.by_date[(RUB, D("2026-01-11"))] = Decimal(90)
    fx.by_date[(RUB, D("2026-03-01"))] = Decimal(100)
    journal.buy(CASE, acc, RUB, 1, 800000, D("2026-01-10"))  # $100
    journal.buy(CASE, acc, RUB, 1, 900000, D("2026-01-11"))  # $100
    assert pos(journal, acc).avg_price_usd == 10000
    journal.sell(CASE, acc, RUB, 2, 1000000, D("2026-03-01"))  # выручка $200
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
    keys = {p.key: p.quantity for p in journal.ledger().open_positions}
    assert keys == {RowKey(CASE, acc, RUB): 1, RowKey(CASE, acc2, RUB): 2, RowKey(CASE, acc, CNY): 3}


def test_months_held(journal, acc):
    journal.buy(CASE, acc, RUB, 1, 1000, D("2026-01-01"))
    journal.sell(CASE, acc, RUB, 1, 2000, D("2026-04-01"))  # 90 дней
    assert journal.ledger().sales[0].months == Decimal("3.0")


def test_cannot_sell_more_than_owned(journal, acc):
    journal.buy(CASE, acc, RUB, 1, 1000, D("2026-01-01"))
    with pytest.raises(JournalError, match="при остатке 1"):
        journal.sell(CASE, acc, RUB, 2, 1000, D("2026-01-02"))
    with pytest.raises(JournalError, match="при остатке 0"):  # продажа раньше покупки
        journal.sell(CASE, acc, RUB, 1, 1000, D("2025-12-31"))


def test_backdated_purchase_changes_profit_of_later_sale(journal, acc):
    journal.buy(CASE, acc, RUB, 1, 10000, D("2026-01-10"))
    journal.sell(CASE, acc, RUB, 1, 20000, D("2026-02-01"))
    journal.buy(CASE, acc, RUB, 1, 30000, D("2026-01-15"))  # внесена позже, но датирована до продажи
    ledger = journal.ledger()
    assert ledger.sales[0].avg_price == 20000
    assert ledger.sales[0].profit == 0
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


def test_transfer_validation_and_rollback(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 5, 1000, D("2026-01-01"))
    with pytest.raises(JournalError, match="при остатке 5"):
        journal.transfer(CASE, acc, acc2, RUB, 6, D("2026-01-02"))
    with pytest.raises(JournalError, match="совпадают"):
        journal.transfer(CASE, acc, acc, RUB, 1, D("2026-01-02"))
    with pytest.raises(JournalError):
        journal.transfer(CASE, acc, 999, RUB, 1, D("2026-01-02"))
    moved = journal.transfer(CASE, acc, acc2, RUB, 5, D("2026-01-02"))
    sale = journal.sell(CASE, acc2, RUB, 2, 1500, D("2026-01-03"))
    with pytest.raises(JournalError, match=f"#{sale}"):  # перенесённое уже продано
        journal.cancel(moved)
    journal.cancel(sale)
    journal.cancel(moved)
    assert (pos(journal, acc).quantity, pos(journal, acc2).quantity) == (5, 0)
    with pytest.raises(JournalError, match="только покупки и продажи"):
        journal.edit(moved, quantity=1)


# ---------- ордера ----------

def test_order_partial_then_full_fill(journal, acc):
    order = journal.place_order(CASE, acc, RUB, 10, 5000)
    journal.fill_order(order, quantity=4)
    ledger = journal.ledger()
    assert ledger.orders[order].quantity == 6
    assert pos(journal, acc).quantity == 4
    journal.fill_order(order, price=4900)  # «Куплено всё», фактическая цена отличалась
    ledger = journal.ledger()
    assert ledger.orders[order].status == "filled"
    assert ledger.open_orders == []
    p = pos(journal, acc)
    assert p.quantity == 10
    assert p.avg_price == Decimal(4 * 5000 + 6 * 4900) / 10


def test_order_fill_cannot_exceed_remaining(journal, acc):
    order = journal.place_order(CASE, acc, RUB, 2, 5000)
    with pytest.raises(JournalError, match="больше остатка"):
        journal.fill_order(order, quantity=3)


def test_order_change_and_remove(journal, acc):
    order = journal.place_order(CASE, acc, RUB, 10, 5000, comment="старт")
    change = journal.change_order(order, price=5500, quantity=8, comment="перебили")
    o = journal.ledger().orders[order]
    assert (o.price, o.quantity, o.comment) == (5500, 8, "перебили")
    journal.cancel(change)
    o = journal.ledger().orders[order]
    assert (o.price, o.quantity, o.comment) == (5000, 10, "старт")
    removed = journal.remove_order(order)
    assert journal.ledger().open_orders == []
    journal.cancel(removed)  # убранный ордер возвращается
    assert [o.order_id for o in journal.ledger().open_orders] == [order]


def test_orders_are_not_in_statistics(journal, acc):
    journal.place_order(CASE, acc, RUB, 10, 5000)
    ledger = journal.ledger()
    assert ledger.invested_usd == 0
    assert ledger.positions == {}


# ---------- откат ----------

def test_rollback_fill_returns_quantity_to_order(journal, acc):
    order = journal.place_order(CASE, acc, RUB, 10, 5000)
    fill = journal.fill_order(order, quantity=10)
    journal.cancel(fill)
    ledger = journal.ledger()
    assert ledger.orders[order].quantity == 10
    assert ledger.orders[order].status == "open"
    assert pos(journal, acc).quantity == 0


def test_rollback_buy_blocked_by_sale_and_names_it(journal, acc):
    buy = journal.buy(CASE, acc, RUB, 5, 1000, D("2026-01-01"))
    sale = journal.sell(CASE, acc, RUB, 3, 2000, D("2026-01-05"))
    preview = journal.preview_cancel(buy)
    assert not preview.allowed
    assert f"#{sale}" in preview.violations[0]
    with pytest.raises(JournalError, match=f"#{sale}"):
        journal.cancel(buy)
    journal.cancel(sale)
    journal.cancel(buy)
    assert pos(journal, acc).quantity == 0
    # отмену можно вернуть — в обратном порядке
    with pytest.raises(JournalError):
        journal.restore(sale)
    journal.restore(buy)
    journal.restore(sale)
    assert pos(journal, acc).quantity == 2


def test_rollback_order_placement_blocked_by_fill(journal, acc):
    order = journal.place_order(CASE, acc, RUB, 10, 5000)
    fill = journal.fill_order(order, quantity=2)
    with pytest.raises(JournalError, match=f"#{fill}"):
        journal.cancel(order)


def test_preview_shows_quantity_average_and_profit_changes(journal, acc):
    journal.buy(CASE, acc, RUB, 1, 10000, D("2026-01-01"))
    second = journal.buy(CASE, acc, RUB, 1, 30000, D("2026-01-02"))
    sale = journal.sell(CASE, acc, RUB, 1, 40000, D("2026-01-03"))
    preview = journal.preview_cancel(second)
    assert preview.allowed
    [change] = preview.positions
    assert (change.quantity_before, change.quantity_after) == (1, 0)
    assert (change.avg_before, change.avg_after) == (20000, None)
    [sale_change] = preview.sales
    assert sale_change.op_id == sale
    assert (sale_change.profit_before, sale_change.profit_after) == (20000, 30000)


# ---------- изменение и удаление ----------

def test_edit_buy_records_before_after_and_can_be_rolled_back(journal, acc):
    buy = journal.buy(CASE, acc, RUB, 2, 10000, D("2026-01-01"))
    edit = journal.edit(buy, price=12000)
    assert pos(journal, acc).avg_price == 12000
    payload = journal.op(edit).payload
    assert (payload["before"]["price"], payload["after"]["price"]) == (10000, 12000)
    journal.cancel(edit)
    assert pos(journal, acc).avg_price == 10000


def test_edit_date_refreshes_rate(journal, fx, acc):
    fx.by_date[(RUB, D("2026-01-05"))] = Decimal(100)
    buy = journal.buy(CASE, acc, RUB, 1, 10000, D("2026-01-01"))
    journal.edit(buy, op_date=D("2026-01-05"))
    assert pos(journal, acc).cost_usd == 100


def test_successive_edits_last_one_wins(journal, acc):
    buy = journal.buy(CASE, acc, RUB, 1, 10000, D("2026-01-01"))
    journal.edit(buy, price=11000)
    second = journal.edit(buy, quantity=3)
    p = pos(journal, acc)
    assert (p.quantity, p.avg_price) == (3, 11000)
    journal.cancel(second)
    p = pos(journal, acc)
    assert (p.quantity, p.avg_price) == (1, 11000)


def test_edit_that_breaks_later_sale_is_rejected(journal, acc):
    buy = journal.buy(CASE, acc, RUB, 5, 1000, D("2026-01-01"))
    journal.sell(CASE, acc, RUB, 4, 2000, D("2026-01-02"))
    with pytest.raises(JournalError, match="при остатке 3"):
        journal.edit(buy, quantity=3)


def test_delete_and_restore(journal, acc):
    buy = journal.buy(CASE, acc, RUB, 1, 1000, D("2026-01-01"))
    deletion = journal.delete(buy)
    assert pos(journal, acc).quantity == 0
    with pytest.raises(JournalError, match="удалена"):
        journal.edit(buy, price=5)
    journal.cancel(deletion)
    assert pos(journal, acc).quantity == 1


def test_edit_sale(journal, acc):
    journal.buy(CASE, acc, RUB, 2, 1000, D("2026-01-01"))
    sale = journal.sell(CASE, acc, RUB, 2, 1500, D("2026-01-02"))
    journal.edit(sale, price=2000)
    assert journal.ledger().sales[0].profit == 2000


def test_edit_fill_limits_fields(journal, acc, acc2):
    order = journal.place_order(CASE, acc, RUB, 5, 1000)
    fill = journal.fill_order(order, quantity=5)
    with pytest.raises(JournalError, match="account_id"):
        journal.edit(fill, account_id=acc2)
    journal.edit(fill, price=900)
    assert pos(journal, acc).avg_price == 900
