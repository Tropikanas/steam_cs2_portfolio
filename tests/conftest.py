from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from steamfolio import db
from steamfolio.currencies import Currency
from steamfolio.journal import Journal, create_account


class FakeFx:
    """Курсы по умолчанию: 80 ₽, 7 ¥, 40 ₴ за доллар; на отдельные даты можно задать свои."""

    def __init__(self):
        self.default = {Currency.RUB: Decimal(80), Currency.CNY: Decimal(7), Currency.UAH: Decimal(40)}
        self.by_date: dict[tuple[Currency, date], Decimal] = {}

    def rate(self, currency: Currency, day: date) -> tuple[Decimal, str]:
        if currency == Currency.USD:
            return Decimal(1), "usd"
        return self.by_date.get((currency, day), self.default[currency]), "steam"


@pytest.fixture
def conn():
    connection = db.connect(Path(":memory:"))
    yield connection
    connection.close()


@pytest.fixture
def fx():
    return FakeFx()


class RowJournal(Journal):
    """Для тестов: продажа, перенос и удаление можно звать и по предмету/аккаунту/валюте, как раньше —
    берётся самая ранняя строка (покупка) этого предмета с достаточным остатком."""

    def lot_for(self, item, account_id, currency, quantity=1):
        rows = sorted((p for p in self.ledger().positions.values() if p.key[:3] == (item, account_id, currency)),
                      key=lambda p: (p.quantity < quantity, p.quantity == 0, p.first_buy_date, p.key.lot))
        return rows[0].key.lot if rows else 10**12  # нет строки — пусть запись сама скажет, что продавать нечего

    def sell(self, *args, **kwargs):
        if isinstance(args[0], str):
            item, account_id, currency, quantity, price, *rest = args
            return super().sell(self.lot_for(item, account_id, currency, quantity), quantity, price, *rest, **kwargs)
        return super().sell(*args, **kwargs)

    def transfer(self, *args, **kwargs):
        if isinstance(args[0], str):
            item, from_account, to_account, currency, quantity, *rest = args
            if rest:
                kwargs.setdefault("op_date", rest[0])
            return super().transfer(self.lot_for(item, from_account, currency, quantity), to_account, quantity, **kwargs)
        return super().transfer(*args, **kwargs)

    def remove_position(self, *args):
        if isinstance(args[0], str):
            return super().remove_position(self.lot_for(*args))
        return super().remove_position(*args)


@pytest.fixture
def journal(conn, fx):
    return RowJournal(conn, fx)


@pytest.fixture
def acc(conn):
    return create_account(conn, "Основной")


@pytest.fixture
def acc2(conn):
    return create_account(conn, "Второй")
