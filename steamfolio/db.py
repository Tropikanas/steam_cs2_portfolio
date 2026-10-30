"""SQLite: подключение и миграции схемы.

Время везде хранится в UTC (ISO 8601); перевод в московское — при показе.
Цены — целые числа в сотых долях валюты (центы, копейки).
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    # 2: пользователи, портфели, аккаунты, журнал операций
    """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY,
        login         TEXT NOT NULL UNIQUE,
        password_hash TEXT,
        created_at    TEXT NOT NULL
    );

    CREATE TABLE portfolios (
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        name       TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE accounts (
        id           INTEGER PRIMARY KEY,
        portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
        name         TEXT NOT NULL,
        hidden       INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL,
        UNIQUE (portfolio_id, name)
    );

    -- Журнал — единственный источник данных: портфель, ордера и прибыль пересчитываются из него.
    -- order_id  — ссылка на операцию order_placed (для операций с ордером);
    -- target_id — ссылка на изменяемую или удаляемую операцию (для *_edited, *_deleted);
    -- payload   — для изменений: {"before": {...}, "after": {...}}.
    -- fx_rate   — сколько единиц валюты операции за 1 USD на дату операции (текстом, без потери точности).
    CREATE TABLE operations (
        id               INTEGER PRIMARY KEY,
        portfolio_id     INTEGER NOT NULL REFERENCES portfolios(id),
        user_id          INTEGER REFERENCES users(id),
        kind             TEXT NOT NULL CHECK (kind IN (
                             'buy', 'sell',
                             'order_placed', 'order_changed', 'order_filled', 'order_removed',
                             'buy_edited', 'buy_deleted', 'sell_edited', 'sell_deleted')),
        op_date          TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        market_hash_name TEXT,
        account_id       INTEGER REFERENCES accounts(id),
        currency         INTEGER,
        quantity         INTEGER,
        price            INTEGER,
        fx_rate          TEXT,
        fx_source        TEXT CHECK (fx_source IN ('usd', 'steam', 'cbr', 'manual')),
        order_id         INTEGER REFERENCES operations(id),
        target_id        INTEGER REFERENCES operations(id),
        payload          TEXT,
        comment          TEXT NOT NULL DEFAULT '',
        cancelled_at     TEXT
    );
    CREATE INDEX operations_portfolio ON operations(portfolio_id, op_date);

    -- История откатов и их отмен.
    CREATE TABLE operation_cancellations (
        id           INTEGER PRIMARY KEY,
        operation_id INTEGER NOT NULL REFERENCES operations(id),
        action       TEXT NOT NULL CHECK (action IN ('cancel', 'restore')),
        user_id      INTEGER REFERENCES users(id),
        at           TEXT NOT NULL
    );

    -- Курсы ЦБ РФ, пересчитанные к USD (единиц валюты за 1 USD), — для дат до начала истории Steam.
    CREATE TABLE fx_cbr (
        date     TEXT NOT NULL,
        currency INTEGER NOT NULL,
        rate     TEXT NOT NULL,
        PRIMARY KEY (date, currency)
    );

    -- Пароль задаётся на этапе 3.
    INSERT INTO users (id, login, created_at) VALUES (1, 'admin', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));
    INSERT INTO portfolios (id, user_id, name, created_at)
        VALUES (1, 1, 'Основной', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));
    """,
    # 3: перенос между аккаунтами, справочник предметов, комментарии строк, вход на сайт
    """
    CREATE TABLE operations_new (
        id               INTEGER PRIMARY KEY,
        portfolio_id     INTEGER NOT NULL REFERENCES portfolios(id),
        user_id          INTEGER REFERENCES users(id),
        kind             TEXT NOT NULL CHECK (kind IN (
                             'buy', 'sell', 'transfer',
                             'order_placed', 'order_changed', 'order_filled', 'order_removed',
                             'buy_edited', 'buy_deleted', 'sell_edited', 'sell_deleted')),
        op_date          TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        market_hash_name TEXT,
        account_id       INTEGER REFERENCES accounts(id),
        to_account_id    INTEGER REFERENCES accounts(id),
        currency         INTEGER,
        quantity         INTEGER,
        price            INTEGER,
        fx_rate          TEXT,
        fx_source        TEXT CHECK (fx_source IN ('usd', 'steam', 'cbr', 'manual')),
        order_id         INTEGER REFERENCES operations(id),
        target_id        INTEGER REFERENCES operations(id),
        payload          TEXT,
        comment          TEXT NOT NULL DEFAULT '',
        cancelled_at     TEXT
    );
    INSERT INTO operations_new (id, portfolio_id, user_id, kind, op_date, created_at, market_hash_name, account_id,
        currency, quantity, price, fx_rate, fx_source, order_id, target_id, payload, comment, cancelled_at)
    SELECT id, portfolio_id, user_id, kind, op_date, created_at, market_hash_name, account_id,
        currency, quantity, price, fx_rate, fx_source, order_id, target_id, payload, comment, cancelled_at
    FROM operations;
    DROP TABLE operations;
    ALTER TABLE operations_new RENAME TO operations;
    CREATE INDEX operations_portfolio ON operations(portfolio_id, op_date);

    -- scheduled — плановый сбор раз в 6 часов; on_demand — цена нового предмета сразу после ввода.
    ALTER TABLE collector_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'scheduled';

    -- Справочник предметов CS2. type_override — тип, заданный вручную.
    CREATE TABLE items (
        market_hash_name TEXT PRIMARY KEY,
        icon_url         TEXT,
        auto_type        TEXT NOT NULL,
        type_override    TEXT,
        updated_at       TEXT NOT NULL
    );

    -- Комментарий строки портфеля (колонка 13).
    CREATE TABLE position_comments (
        portfolio_id     INTEGER NOT NULL REFERENCES portfolios(id),
        market_hash_name TEXT NOT NULL,
        account_id       INTEGER NOT NULL REFERENCES accounts(id),
        currency         INTEGER NOT NULL,
        comment          TEXT NOT NULL,
        PRIMARY KEY (portfolio_id, market_hash_name, account_id, currency)
    );

    -- Сессии входа: храним только хэш токена из cookie.
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );

    CREATE TABLE login_attempts (
        id      INTEGER PRIMARY KEY,
        login   TEXT NOT NULL,
        ip      TEXT NOT NULL,
        at      TEXT NOT NULL,
        success INTEGER NOT NULL
    );
    CREATE INDEX login_attempts_at ON login_attempts(at);
    """,
    # 4: история цен Steam для оценки стоимости портфеля до начала собственных снимков
    """
    -- Медианная цена продажи за день, пересчитанная в центы USD по курсу ЦБ на тот день.
    CREATE TABLE price_history (
        market_hash_name TEXT NOT NULL,
        day              TEXT NOT NULL,
        price_usd        INTEGER NOT NULL,
        volume           INTEGER,
        PRIMARY KEY (market_hash_name, day)
    );

    -- Для каких предметов история уже загружена (загружается один раз).
    CREATE TABLE price_history_fetched (
        market_hash_name TEXT PRIMARY KEY,
        fetched_at       TEXT NOT NULL,
        points           INTEGER NOT NULL,
        error            TEXT
    );
    """,
    # 5: настройки, которые меняются с сайта; уведомления в Telegram
    """
    -- Пороги уведомлений, чат Telegram, отметки о бэкапах. Значения — JSON.
    CREATE TABLE app_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Отправленные уведомления о ценах — для антиспама и истории.
    CREATE TABLE alerts_sent (
        id               INTEGER PRIMARY KEY,
        market_hash_name TEXT NOT NULL,
        rule             TEXT NOT NULL,
        sent_at          TEXT NOT NULL,
        price_then       INTEGER NOT NULL,
        price_now        INTEGER NOT NULL
    );
    CREATE INDEX alerts_sent_item ON alerts_sent(market_hash_name, rule, sent_at);
    """,
    # 6: операция «позиция удалена» (кнопка «Удалить позицию» в портфеле)
    """
    CREATE TABLE operations_new (
        id               INTEGER PRIMARY KEY,
        portfolio_id     INTEGER NOT NULL REFERENCES portfolios(id),
        user_id          INTEGER REFERENCES users(id),
        kind             TEXT NOT NULL CHECK (kind IN (
                             'buy', 'sell', 'transfer', 'position_removed',
                             'order_placed', 'order_changed', 'order_filled', 'order_removed',
                             'buy_edited', 'buy_deleted', 'sell_edited', 'sell_deleted')),
        op_date          TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        market_hash_name TEXT,
        account_id       INTEGER REFERENCES accounts(id),
        to_account_id    INTEGER REFERENCES accounts(id),
        currency         INTEGER,
        quantity         INTEGER,
        price            INTEGER,
        fx_rate          TEXT,
        fx_source        TEXT CHECK (fx_source IN ('usd', 'steam', 'cbr', 'manual')),
        order_id         INTEGER REFERENCES operations(id),
        target_id        INTEGER REFERENCES operations(id),
        payload          TEXT,
        comment          TEXT NOT NULL DEFAULT '',
        cancelled_at     TEXT
    );
    INSERT INTO operations_new SELECT id, portfolio_id, user_id, kind, op_date, created_at, market_hash_name, account_id,
        to_account_id, currency, quantity, price, fx_rate, fx_source, order_id, target_id, payload, comment, cancelled_at
    FROM operations;
    DROP TABLE operations;
    ALTER TABLE operations_new RENAME TO operations;
    CREATE INDEX operations_portfolio ON operations(portfolio_id, op_date);
    """,
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row[0])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        if value is None:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute(
                """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, json.dumps(value, ensure_ascii=False), utcnow()),
            )


def connect(path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(MIGRATIONS):
        return
    # Пересоздание таблиц (как operations в миграции 3) требует выключенных внешних ключей.
    conn.execute("PRAGMA foreign_keys = OFF")
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("PRAGMA foreign_keys = ON")
    if broken:
        raise RuntimeError(f"после миграции нарушены внешние ключи: {broken[:5]}")
