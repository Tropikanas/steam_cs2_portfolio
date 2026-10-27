"""Один запуск сборщика: курсы Steam, затем цены всех отслеживаемых предметов."""

import logging
import sqlite3
from dataclasses import dataclass

from steamfolio.currencies import RATE_CURRENCIES, USER_CURRENCIES, Currency
from steamfolio.db import utcnow
from steamfolio.ledger import Op, replay
from steamfolio.market import Market, MarketError
from steamfolio.rates import compute_rates, convert
from steamfolio.settings import Settings

log = logging.getLogger(__name__)

# Столько отказов подряд — признак блокировки, дальше не мучаем Steam.
MAX_CONSECUTIVE_FAILURES = 5


@dataclass(frozen=True)
class Target:
    market_hash_name: str
    currencies: tuple[Currency, ...]


def targets(conn: sqlite3.Connection) -> list[Target]:
    """Предметы портфеля и ордеров — в валютах их строк и в USD; tracked_items — во всех валютах."""
    needed: dict[str, set[Currency]] = {}
    for (portfolio_id,) in conn.execute("SELECT id FROM portfolios").fetchall():
        rows = conn.execute("SELECT * FROM operations WHERE portfolio_id = ?", (portfolio_id,)).fetchall()
        ledger = replay([Op.from_row(r) for r in rows])
        for item, currency in [(p.key.market_hash_name, p.key.currency) for p in ledger.open_positions] + [
            (o.market_hash_name, o.currency) for o in ledger.open_orders
        ]:
            needed.setdefault(item, set()).update((currency, Currency.USD))
    for (item,) in conn.execute("SELECT market_hash_name FROM tracked_items ORDER BY rowid").fetchall():
        needed.setdefault(item, set()).update(USER_CURRENCIES)
    return [
        Target(item, tuple(c for c in USER_CURRENCIES if c in currencies)) for item, currencies in needed.items()
    ]


def latest_rates(conn: sqlite3.Connection) -> dict[Currency, float]:
    rows = conn.execute(
        """SELECT currency, rate FROM fx_snapshots f
           WHERE taken_at = (SELECT MAX(taken_at) FROM fx_snapshots WHERE currency = f.currency)"""
    ).fetchall()
    return {Currency(row[0]): row[1] for row in rows}


def run(conn: sqlite3.Connection, market: Market, settings: Settings) -> int:
    with conn:
        run_id = conn.execute(
            "INSERT INTO collector_runs (started_at, status) VALUES (?, 'running')", (utcnow(),)
        ).lastrowid
    problems: list[str] = []
    try:
        rates_ok = _collect_rates(conn, market, settings, run_id, problems)
        total, ok = _collect_prices(conn, market, run_id, problems)
    except Exception as e:
        _finish(conn, run_id, "failed", 0, 0, False, problems + [f"сбой: {e!r}"])
        raise
    if ok == 0 and total > 0:
        status = "failed"
    elif ok < total or not rates_ok:
        status = "partial"
    else:
        status = "ok"
    _finish(conn, run_id, status, total, ok, rates_ok, problems)
    log.info("Запуск %d: %s, цен %d/%d, курсы %s", run_id, status, ok, total, "есть" if rates_ok else "НЕТ")
    return run_id


def run_on_demand(conn: sqlite3.Connection, market: Market, wanted: list[Target]) -> int:
    """Цены только что внесённых предметов — не дожидаясь планового сбора. Курсы не снимает."""
    with conn:
        run_id = conn.execute(
            "INSERT INTO collector_runs (started_at, status, kind) VALUES (?, 'running', 'on_demand')", (utcnow(),)
        ).lastrowid
    problems: list[str] = []
    try:
        total, ok = _collect_prices(conn, market, run_id, problems, wanted)
    except Exception as e:
        _finish(conn, run_id, "failed", 0, 0, False, problems + [f"сбой: {e!r}"])
        raise
    _finish(conn, run_id, "ok" if ok == total else "partial" if ok else "failed", total, ok, False, problems)
    return run_id


def missing_prices(conn: sqlite3.Connection, item: str, currency: Currency) -> Target | None:
    """Что снять сразу для нового предмета: валюта строки и USD, если по ним ещё нет ни одного снимка."""
    missing = tuple(
        c for c in USER_CURRENCIES if c in (currency, Currency.USD) and conn.execute(
            "SELECT 1 FROM price_snapshots WHERE market_hash_name = ? AND currency = ? LIMIT 1", (item, int(c))
        ).fetchone() is None
    )
    return Target(item, missing) if missing else None


def _collect_rates(conn: sqlite3.Connection, market: Market, settings: Settings, run_id: int, problems: list[str]) -> bool:
    prices: dict[str, dict[Currency, int | None]] = {}
    for name in settings.rate_items:
        prices[name] = {}
        for currency in (Currency.USD, *RATE_CURRENCIES):
            try:
                quote = market.quote(name, currency)
            except MarketError as e:
                problems.append(f"курс, {name} {currency.name}: {e}")
                continue
            if quote.currency != currency:
                # Запасной путь отдаёт одну валюту — для курса он бесполезен.
                problems.append(f"курс, {name}: нет item_nameid, предмет пропущен")
                break
            prices[name][currency] = quote.lowest_sell
    rates, rate_problems = compute_rates(
        prices, RATE_CURRENCIES, round(settings.min_rate_item_usd * 100), settings.min_rate_samples
    )
    problems.extend(rate_problems)
    now = utcnow()
    with conn:
        conn.executemany(
            "INSERT INTO fx_samples (run_id, market_hash_name, currency, lowest_sell) VALUES (?, ?, ?, ?)",
            [(run_id, name, int(c), price) for name, by_c in prices.items() for c, price in by_c.items()],
        )
        conn.executemany(
            "INSERT INTO fx_snapshots (run_id, taken_at, currency, rate, samples, spread_pct) VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, now, int(r.currency), r.rate, r.samples, r.spread_pct) for r in rates.values()],
        )
    for r in rates.values():
        log.info("Курс %s: %.4f (предметов %d, разброс %.2f%%)", r.currency.name, r.rate, r.samples, r.spread_pct)
    return len(rates) == len(RATE_CURRENCIES)


def _collect_prices(
    conn: sqlite3.Connection, market: Market, run_id: int, problems: list[str], wanted: list[Target] | None = None
) -> tuple[int, int]:
    rates = latest_rates(conn)
    total = ok = consecutive_failures = 0
    for target in targets(conn) if wanted is None else wanted:
        for currency in target.currencies:
            total += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                continue
            try:
                quote = market.quote(target.market_hash_name, currency)
            except MarketError as e:
                consecutive_failures += 1
                problems.append(f"{target.market_hash_name} {currency.name}: {e}")
                continue
            best_buy, lowest_sell, source = quote.best_buy, quote.lowest_sell, quote.source
            if quote.currency != currency:
                known = {Currency.USD, *rates}
                if quote.currency not in known or currency not in known:
                    consecutive_failures += 1
                    problems.append(f"{target.market_hash_name} {currency.name}: нет курса для пересчёта")
                    continue
                best_buy = convert(best_buy, quote.currency, currency, rates)
                lowest_sell = convert(lowest_sell, quote.currency, currency, rates)
                source = f"{source}_converted"
            consecutive_failures = 0
            ok += 1
            with conn:
                conn.execute(
                    """INSERT INTO price_snapshots
                       (run_id, taken_at, market_hash_name, currency, best_buy, lowest_sell, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, utcnow(), target.market_hash_name, int(currency), best_buy, lowest_sell, source),
                )
    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        problems.append(f"{MAX_CONSECUTIVE_FAILURES} отказов подряд — запуск прерван (возможна блокировка IP)")
    return total, ok


def _finish(
    conn: sqlite3.Connection, run_id: int, status: str, total: int, ok: int, rates_ok: bool, problems: list[str]
) -> None:
    with conn:
        conn.execute(
            """UPDATE collector_runs SET finished_at = ?, status = ?, quotes_total = ?, quotes_ok = ?,
               rates_ok = ?, error = ? WHERE id = ?""",
            (utcnow(), status, total, ok, int(rates_ok), "\n".join(problems) or None, run_id),
        )
