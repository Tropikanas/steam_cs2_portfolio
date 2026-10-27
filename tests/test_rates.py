from steamfolio.currencies import Currency
from steamfolio.rates import compute_rates, convert

RUB, USD, UAH = Currency.RUB, Currency.USD, Currency.UAH


def test_rate_is_median_of_expensive_items():
    prices = {
        # Karambit | Doppler (FN) из ТЗ: $1 500.00 → 120 767,45 ₽
        "a": {USD: 150000, RUB: 12076745},
        "b": {USD: 100000, RUB: 8052000},
        "c": {USD: 80000, RUB: 6440000},
        # кейс за $0.12: округление даёт неверный курс, предмет отбрасывается порогом
        "case": {USD: 12, RUB: 1100},
    }
    rates, problems = compute_rates(prices, (RUB,), min_usd_minor=50000, min_samples=3)
    assert round(rates[RUB].rate, 3) == 80.512
    assert rates[RUB].samples == 3
    assert any("case" in p for p in problems)


def test_not_enough_samples_gives_no_rate():
    prices = {"a": {USD: 150000, UAH: 6816800}, "b": {USD: 150000, UAH: None}}
    rates, problems = compute_rates(prices, (UAH,), min_usd_minor=50000, min_samples=3)
    assert UAH not in rates
    assert problems


def test_convert_through_usd():
    rates = {RUB: 80.0, Currency.EUR: 0.8}
    assert convert(100, Currency.EUR, RUB, rates) == 10000  # €1 = $1.25 = 100 ₽
    assert convert(150000, USD, RUB, rates) == 12000000
    assert convert(None, USD, RUB, rates) is None
