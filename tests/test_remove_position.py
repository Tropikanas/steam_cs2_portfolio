from datetime import date

import pytest

from steamfolio import db
from steamfolio.currencies import Currency
from steamfolio.journal import JournalError
from steamfolio.ledger import RowKey

RUB = Currency.RUB
CASE = "Kilowatt Case"
D = date.fromisoformat


def test_remove_rest_after_sale_reduces_invested_without_profit(journal, acc):
    journal.buy(CASE, acc, RUB, 10, 8000, D("2026-09-01"))  # $10.00 по курсу 80
    journal.sell(CASE, acc, RUB, 4, 16000, D("2026-09-10"))  # прибыль $4.00
    op_id = journal.remove_position(CASE, acc, RUB)
    ledger = journal.ledger()
    assert ledger.positions[RowKey(CASE, acc, RUB)].quantity == 0
    assert ledger.open_positions == []
    assert ledger.invested_usd == 400  # $10.00 − остаток 6 шт. по $1.00
    assert ledger.profit_usd == 400 and len(ledger.sales) == 1  # продажа не изменилась
    assert journal.op(op_id).quantity == 6

    journal.cancel(op_id)  # откат возвращает позицию и вложения
    ledger = journal.ledger()
    assert ledger.positions[RowKey(CASE, acc, RUB)].quantity == 6
    assert ledger.invested_usd == 1000


def test_remove_only_one_account(journal, acc, acc2):
    journal.buy(CASE, acc, RUB, 5, 8000, D("2026-09-01"))
    journal.transfer(CASE, acc, acc2, RUB, 2, D("2026-09-02"))
    journal.remove_position(CASE, acc2, RUB)
    ledger = journal.ledger()
    assert ledger.positions[RowKey(CASE, acc, RUB)].quantity == 3
    assert ledger.positions[RowKey(CASE, acc2, RUB)].quantity == 0
    assert ledger.invested_usd == 300


def test_cannot_remove_empty_position(journal, acc):
    with pytest.raises(JournalError):
        journal.remove_position(CASE, acc, RUB)


def test_backdated_sale_after_removal_is_rejected(journal, acc):
    journal.buy(CASE, acc, RUB, 3, 8000, D("2026-09-01"))
    journal.remove_position(CASE, acc, RUB)
    with pytest.raises(JournalError):
        journal.sell(CASE, acc, RUB, 1, 8000, D("2026-09-05"))  # раньше удаления — тогда удалять было бы нечего


def test_migration_keeps_operations(tmp_path):
    from pathlib import Path

    conn = db.connect(Path(tmp_path / "m.db"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(operations)")]
    assert "to_account_id" in columns and columns[-1] == "cancelled_at"
    conn.close()
