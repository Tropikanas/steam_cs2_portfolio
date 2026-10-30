from enum import IntEnum


class Currency(IntEnum):
    """Коды валют Steam (ECurrency). Все цены Steam отдаёт в сотых долях: 9,44 ₽ = 944 (и для иены, и для вона).

    Список проверен запросами к торговой площадке 31.10.2026: коды 33 и 36 (BYN) цен не отдают.
    """

    USD = 1
    GBP = 2
    EUR = 3
    CHF = 4
    RUB = 5
    PLN = 6
    BRL = 7
    JPY = 8
    NOK = 9
    IDR = 10
    MYR = 11
    PHP = 12
    SGD = 13
    THB = 14
    VND = 15
    KRW = 16
    TRY = 17
    UAH = 18
    MXN = 19
    CAD = 20
    AUD = 21
    NZD = 22
    CNY = 23
    INR = 24
    CLP = 25
    PEN = 26
    COP = 27
    ZAR = 28
    HKD = 29
    TWD = 30
    SAR = 31
    AED = 32
    ARS = 34
    ILS = 35
    KZT = 37
    KWD = 38
    QAR = 39
    CRC = 40
    UYU = 41


# Знак валюты для показа (USD — перед суммой, остальные — после).
SYMBOLS: dict[Currency, str] = {
    Currency.USD: "$", Currency.GBP: "£", Currency.EUR: "€", Currency.CHF: "CHF", Currency.RUB: "₽",
    Currency.PLN: "zł", Currency.BRL: "R$", Currency.JPY: "JP¥", Currency.NOK: "kr", Currency.IDR: "Rp",
    Currency.MYR: "RM", Currency.PHP: "₱", Currency.SGD: "S$", Currency.THB: "฿", Currency.VND: "₫",
    Currency.KRW: "₩", Currency.TRY: "TL", Currency.UAH: "₴", Currency.MXN: "Mex$", Currency.CAD: "CDN$",
    Currency.AUD: "A$", Currency.NZD: "NZ$", Currency.CNY: "¥", Currency.INR: "₹", Currency.CLP: "CLP$",
    Currency.PEN: "S/.", Currency.COP: "COL$", Currency.ZAR: "R", Currency.HKD: "HK$", Currency.TWD: "NT$",
    Currency.SAR: "SR", Currency.AED: "AED", Currency.ARS: "ARS$", Currency.ILS: "₪", Currency.KZT: "₸",
    Currency.KWD: "KD", Currency.QAR: "QR", Currency.CRC: "₡", Currency.UYU: "$U",
}

NAMES: dict[Currency, str] = {
    Currency.USD: "доллар США", Currency.GBP: "фунт стерлингов", Currency.EUR: "евро",
    Currency.CHF: "швейцарский франк", Currency.RUB: "российский рубль", Currency.PLN: "польский злотый",
    Currency.BRL: "бразильский реал", Currency.JPY: "японская иена", Currency.NOK: "норвежская крона",
    Currency.IDR: "индонезийская рупия", Currency.MYR: "малайзийский ринггит", Currency.PHP: "филиппинское песо",
    Currency.SGD: "сингапурский доллар", Currency.THB: "тайский бат", Currency.VND: "вьетнамский донг",
    Currency.KRW: "южнокорейская вона", Currency.TRY: "турецкая лира", Currency.UAH: "украинская гривна",
    Currency.MXN: "мексиканское песо", Currency.CAD: "канадский доллар", Currency.AUD: "австралийский доллар",
    Currency.NZD: "новозеландский доллар", Currency.CNY: "китайский юань", Currency.INR: "индийская рупия",
    Currency.CLP: "чилийское песо", Currency.PEN: "перуанский соль", Currency.COP: "колумбийское песо",
    Currency.ZAR: "южноафриканский рэнд", Currency.HKD: "гонконгский доллар", Currency.TWD: "тайваньский доллар",
    Currency.SAR: "саудовский риял", Currency.AED: "дирхам ОАЭ", Currency.ARS: "аргентинское песо",
    Currency.ILS: "израильский шекель", Currency.KZT: "казахстанский тенге", Currency.KWD: "кувейтский динар",
    Currency.QAR: "катарский риал", Currency.CRC: "костариканский колон", Currency.UYU: "уругвайское песо",
}

# Основные валюты — первыми в списках; tracked_items снимаются только в них.
MAIN_CURRENCIES: tuple[Currency, ...] = (Currency.RUB, Currency.USD, Currency.CNY, Currency.UAH)

# Все валюты, в которых пользователь может покупать и продавать: основные, затем остальные по коду.
USER_CURRENCIES: tuple[Currency, ...] = MAIN_CURRENCIES + tuple(
    sorted((c for c in Currency if c not in MAIN_CURRENCIES), key=lambda c: c.name)
)

# Курс Steam к USD считается всегда для этих валют (EUR — валюта запасного пути Market Beta)
# и дополнительно для каждой валюты, которая есть в портфеле или ордерах.
BASE_RATE_CURRENCIES: tuple[Currency, ...] = (Currency.RUB, Currency.CNY, Currency.UAH, Currency.EUR)
RATE_CURRENCIES = BASE_RATE_CURRENCIES  # для отчёта по умолчанию


def rate_currencies(used: set[Currency] | frozenset[Currency] = frozenset()) -> tuple[Currency, ...]:
    extra = sorted((c for c in used if c != Currency.USD and c not in BASE_RATE_CURRENCIES), key=lambda c: c.name)
    return BASE_RATE_CURRENCIES + tuple(extra)


def format_minor(amount: int | None) -> str:
    if amount is None:
        return "—"
    return f"{amount / 100:,.2f}".replace(",", " ")
