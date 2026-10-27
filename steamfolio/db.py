"""SQLite: подключение и миграции схемы.

Время везде хранится в UTC (ISO 8601); перевод в московское — при показе.
Цены — целые числа в сотых долях валюты (центы, копейки).
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: сборщик цен и курсов
    """
    CREATE TABLE collector_runs (
        id            INTEGER PRIMARY KEY,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        status        TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
        quotes_total  INTEGER NOT NULL DEFAULT 0,
        quotes_ok     INTEGER NOT NULL DEFAULT 0,
        rates_ok      INTEGER NOT NULL DEFAULT 0,
        error         TEXT
    );

    -- Курс Steam: сколько единиц валюты за 1 USD.
    CREATE TABLE fx_snapshots (
        run_id     INTEGER NOT NULL REFERENCES collector_runs(id),
        taken_at   TEXT NOT NULL,
        currency   INTEGER NOT NULL,
        rate       REAL NOT NULL,
        samples    INTEGER NOT NULL,
        spread_pct REAL NOT NULL,
        PRIMARY KEY (run_id, currency)
    );
    CREATE INDEX fx_snapshots_currency_time ON fx_snapshots(currency, taken_at);

    -- Цены лотов эталонных предметов, из которых посчитан курс (для проверки).
    CREATE TABLE fx_samples (
        run_id           INTEGER NOT NULL REFERENCES collector_runs(id),
        market_hash_name TEXT NOT NULL,
        currency         INTEGER NOT NULL,
        lowest_sell      INTEGER,
        PRIMARY KEY (run_id, market_hash_name, currency)
    );

    -- source: histogram — запрос в нужной валюте;
    --         orderbook_converted — запасной путь Market Beta, пересчитано по курсу Steam.
    CREATE TABLE price_snapshots (
        run_id           INTEGER NOT NULL REFERENCES collector_runs(id),
        taken_at         TEXT NOT NULL,
        market_hash_name TEXT NOT NULL,
        currency         INTEGER NOT NULL,
        best_buy         INTEGER,
        lowest_sell      INTEGER,
        source           TEXT NOT NULL,
        PRIMARY KEY (run_id, market_hash_name, currency)
    );
    CREATE INDEX price_snapshots_item_time ON price_snapshots(market_hash_name, currency, taken_at);

    -- source: dataset — открытый справочник; manual — задано вручную, справочник не перезаписывает.
    CREATE TABLE item_nameids (
        market_hash_name TEXT PRIMARY KEY,
        item_nameid      INTEGER NOT NULL,
        source           TEXT NOT NULL CHECK (source IN ('dataset', 'manual')),
        updated_at       TEXT NOT NULL
    );

    -- Предметы, которые сборщик отслеживает помимо портфеля и ордеров (на этапе 1 — тестовые).
    CREATE TABLE tracked_items (
        market_hash_name TEXT PRIMARY KEY,
        comment          TEXT NOT NULL DEFAULT '',
        added_at         TEXT NOT NULL
    );
    """,
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
