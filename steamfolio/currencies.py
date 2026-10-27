from enum import IntEnum


class Currency(IntEnum):
    """Коды валют Steam (ECurrency). Все цены Steam отдаёт в сотых долях: 9,44 ₽ = 944."""

    USD = 1
    EUR = 3  # не валюта пользователя: в ней Steam отвечает серверу из Нидерландов
    RUB = 5
    UAH = 18
    CNY = 23


# Валюты, в которых пользователь покупает и продаёт (ТЗ: RUB, USD, CNY, UAH).
USER_CURRENCIES: tuple[Currency, ...] = (Currency.RUB, Currency.USD, Currency.CNY, Currency.UAH)

# Курсы к USD считаем для всех валют, кроме самого USD.
RATE_CURRENCIES: tuple[Currency, ...] = (Currency.RUB, Currency.CNY, Currency.UAH, Currency.EUR)


def format_minor(amount: int | None) -> str:
    if amount is None:
        return "—"
    return f"{amount / 100:,.2f}".replace(",", " ")
