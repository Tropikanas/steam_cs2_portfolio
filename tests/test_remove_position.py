from datetime import date

import pytest

from steamfolio import db
from steamfolio.currencies import Currency
from steamfolio.journal import JournalError
from steamfolio.ledger import RowKey

RUB = Currency.RUB
CASE = "Kilowatt Case"
D = date.fromisoformat


def kinds(journal):
    return [(op.kind, op.market_hash_name, op.account_id, op.quantity) for op in journal.ops()]


def test_remove_rest_after_sale_erases_rest_of_purchase(journal, acc):
    journal.buy(CASE, acc, RUB, 10, 8000, D("2026-09-01"))  # $10.00 по курсу 80
    journal.sell(CASE, acc, RUB, 4, 16000, D("2026-09-10"))  # прибыль $4.00
    assert journal.remove_position(CASE, acc, RUB) == 6
    # остаток стёрт из самой покупки — будто купили 4 шт.
    assert kinds(journal) == [("buy", CASE, acc, 4), ("sell", CASE, acc, 4)]
    ledger = journal.ledger()
    assert ledger.open_positions == []
    assert ledger.invested_usd == 400
    assert ledger.profit_usd == 400 and len(ledger.sales) == 1  # продажа осталась


def test_remove_takes_latest_purchases_first(journal, acc):
    journal.buy(CASE, acc, RUB, 10, 8000, D("2026-09-01"))
    journal.buy(CASE, acc, RUB, 10, 8000, D("2026-09-05"))
    journal.sell(CASE, acc, RUB, 5, 8000, D("2026-09-10"))
    journal.remove_position(CASE, acc, RUB)  # 15: сентябрьские 10 от 05.09 и 5 от 01.09
    assert kinds(journal) == [("buy", CASE, acc, 5), ("sell", CASE, acc, 5)]


def test_remove_transferred_row_erases_it_at_source_too(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 5, 8000, D("2026-09-01"))
    journal.transfer(CASE, acc, acc2, RUB, 2)
    journal.remove_position(CASE, acc2, RUB)
    # перенос и привезённая покупка стёрты, исходная покупка — 3 шт.
    assert kinds(journal) == [("buy", CASE, acc, 3)]
    ledger = journal.ledger()
    assert ledger.positions[RowKey(CASE, acc, RUB)].quantity == 3
    assert ledger.invested_usd == 300


def test_removed_positions_not_in_closed(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 5, 8000, D("2026-09-01"))
    journal.remove_position(CASE, acc, RUB)  # ошибочная — её будто не было
    journal.buy("AK", acc, RUB, 5, 8000, D("2026-09-01"))
    journal.sell("AK", acc, RUB, 2, 8000, D("2026-09-02"))
    journal.remove_position("AK", acc, RUB)  # часть продана — позиция была настоящей
    journal.buy("M4", acc, RUB, 1, 8000, D("2026-09-01"))
    journal.sell("M4", acc, RUB, 1, 8000, D("2026-09-02"))
    closed = {(p.key.market_hash_name, p.key.account_id) for p in journal.ledger().closed_positions}
    assert closed == {("AK", acc), ("M4", acc)}


def test_cannot_remove_empty_position(journal, acc):
    with pytest.raises(JournalError):
        journal.remove_position(CASE, acc, RUB)


def test_backdated_sale_after_removal_is_rejected(journal, acc):
    journal.buy(CASE, acc, RUB, 3, 8000, D("2026-09-01"))
    journal.remove_position(CASE, acc, RUB)
    with pytest.raises(JournalError):
        journal.sell(CASE, acc, RUB, 1, 8000, D("2026-09-05"))  # покупки больше нет — продавать нечего


def test_migrations_create_records_tables(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(operations)")]
    assert "to_account_id" in columns
    assert {r[1] for r in conn.execute("PRAGMA table_info(orders)")} >= {"market_hash_name", "quantity", "comment"}
    conn.close()
