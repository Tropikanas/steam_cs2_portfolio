import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx

from steamfolio.db import utcnow
from steamfolio.market.http import USER_AGENT

log = logging.getLogger(__name__)


def get_nameid(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT item_nameid FROM item_nameids WHERE market_hash_name = ?", (name,)).fetchone()
    return row[0] if row else None


def set_manual(conn: sqlite3.Connection, name: str, item_nameid: int) -> None:
    with conn:
        conn.execute(
            """INSERT INTO item_nameids (market_hash_name, item_nameid, source, updated_at)
               VALUES (?, ?, 'manual', ?)
               ON CONFLICT (market_hash_name) DO UPDATE
               SET item_nameid = excluded.item_nameid, source = 'manual', updated_at = excluded.updated_at""",
            (name, item_nameid, utcnow()),
        )


def is_stale(conn: sqlite3.Connection, max_age_hours: int) -> bool:
    row = conn.execute("SELECT MAX(updated_at) FROM item_nameids WHERE source = 'dataset'").fetchone()
    if not row[0]:
        return True
    return datetime.fromisoformat(row[0]) < datetime.now(UTC) - timedelta(hours=max_age_hours)


def refresh(conn: sqlite3.Connection, url: str, proxy: str | None = None) -> int:
    """Загружает справочник item_nameid. Ручные значения не трогает."""
    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=120, proxy=proxy, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or len(data) < 1000:
        raise ValueError(f"справочник item_nameid выглядит повреждённым: {len(data)} записей")
    now = utcnow()
    with conn:
        conn.executemany(
            """INSERT INTO item_nameids (market_hash_name, item_nameid, source, updated_at)
               VALUES (?, ?, 'dataset', ?)
               ON CONFLICT (market_hash_name) DO UPDATE
               SET item_nameid = excluded.item_nameid, updated_at = excluded.updated_at
               WHERE item_nameids.source = 'dataset'""",
            ((name, int(nid), now) for name, nid in data.items()),
        )
    log.info("Справочник item_nameid обновлён: %d предметов", len(data))
    return len(data)
