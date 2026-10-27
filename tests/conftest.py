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


@pytest.fixture
def journal(conn, fx):
    return Journal(conn, fx)


@pytest.fixture
def acc(conn):
    return create_account(conn, "Основной")


@pytest.fixture
def acc2(conn):
    return create_account(conn, "Второй")
