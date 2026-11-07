"""Справочник предметов CS2: название как на площадке, иконка, тип для круговой диаграммы.

Источник — открытая база ByMykel/CSGO-API (иконки — CDN Steam). Предметы, которых в ней нет,
но есть в справочнике item_nameid, тоже попадают в подсказки — с типом «Прочее» и без иконки.
"""

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from steamfolio.db import utcnow

log = logging.getLogger(__name__)

BASE_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/"

CASES = "Кейсы"
CAPSULES = "Капсулы с наклейками"
SOUVENIRS = "Сувенирные наборы"
TOURNAMENT_STICKERS = "Наклейки турниров"
OPERATION_STICKERS = "Наклейки операций"
OTHER_STICKERS = "Наклейки иные"
PATCHES = "Нашивки"
AGENTS = "Агенты"
WEAPONS = "Оружие"
KNIVES = "Ножи"
GLOVES = "Перчатки"
OTHER = "Прочее"
# Тип предмета (его можно поменять вручную) — самый мелкий уровень; категория диаграммы выводится из него.
TYPES = (CASES, CAPSULES, SOUVENIRS, TOURNAMENT_STICKERS, OPERATION_STICKERS, OTHER_STICKERS, PATCHES, AGENTS,
         WEAPONS, KNIVES, GLOVES, OTHER)

STICKERS = "Наклейки"
KNIVES_GLOVES = "Ножи и перчатки"
# Категории круговой диаграммы по порядку; у части — подкатегории (у агентов и оружия — по редкости).
CATEGORIES = (CASES, CAPSULES, SOUVENIRS, STICKERS, PATCHES, AGENTS, WEAPONS, KNIVES_GLOVES, OTHER)
PARENT = {TOURNAMENT_STICKERS: STICKERS, OPERATION_STICKERS: STICKERS, OTHER_STICKERS: STICKERS,
          KNIVES: KNIVES_GLOVES, GLOVES: KNIVES_GLOVES}
BY_RARITY = (AGENTS, WEAPONS)

# Редкость (как в базе ByMykel, по-английски) → название и цвет на Торговой площадке Steam, от низшей к высшей.
RARITIES = {
    "Consumer Grade": ("Ширпотреб", "#b0c3d9"),
    "Industrial Grade": ("Промышленное качество", "#5e98d9"),
    "Mil-Spec Grade": ("Армейское качество", "#4b69ff"),
    "Restricted": ("Запрещённое", "#8847ff"),
    "Classified": ("Засекреченное", "#d32ce6"),
    "Covert": ("Тайное", "#eb4b4b"),
    "Contraband": ("Контрабанда", "#e4ae39"),
    "Extraordinary": ("Экстраординарного типа", "#eb4b4b"),
    "Distinguished": ("Заслуженный", "#4b69ff"),
    "Exceptional": ("Исключительный", "#8847ff"),
    "Superior": ("Превосходный", "#d32ce6"),
    "Master": ("Мастер", "#eb4b4b"),
}
UNKNOWN_RARITY = ("Редкость неизвестна", "#9ca3af")


def category(kind: str) -> str:
    return PARENT.get(kind, kind)


def subcategory(kind: str, rarity: str | None) -> tuple[str, str | None, int] | None:
    """(подкатегория, цвет редкости или None, порядок) — или None, если у категории подкатегорий нет."""
    if kind in BY_RARITY:
        names = list(RARITIES)
        name, color = RARITIES.get(rarity or "", UNKNOWN_RARITY)
        return name, color, names.index(rarity) if rarity in RARITIES else len(names)
    if kind in PARENT:
        return kind, None, TYPES.index(kind)
    return None

FILES = ("crates.json", "stickers.json", "skins_not_grouped.json", "agents.json", "patches.json", "keychains.json",
         "graffiti.json", "music_kits.json", "keys.json", "collectibles.json", "highlights.json")


def _names(value: Any) -> list[str]:
    """crates/collections бывают списком словарей, а бывают строкой с его repr."""
    if isinstance(value, list):
        return [str(v.get("name", "")) for v in value if isinstance(v, dict)]
    return [value] if isinstance(value, str) else []


def classify(file: str, item: dict[str, Any]) -> str:
    if file == "crates.json":
        kind = item.get("type")
        if kind == "Case":
            return CASES
        if kind in ("Sticker Capsule", "Autograph Capsule"):
            return CAPSULES
        if kind in ("Souvenir", "Souvenir Highlight"):
            return SOUVENIRS
        return OTHER
    if file == "stickers.json":
        if item.get("tournament"):
            return TOURNAMENT_STICKERS
        sources = _names(item.get("crates")) + _names(item.get("collections"))
        if any("Operation" in s for s in sources):
            return OPERATION_STICKERS
        return OTHER_STICKERS
    if file == "skins_not_grouped.json":
        category = (item.get("category") or {}).get("name")
        return {"Knives": KNIVES, "Gloves": GLOVES}.get(category, WEAPONS)
    if file == "agents.json":
        return AGENTS
    if file == "patches.json":
        return PATCHES
    return OTHER


def parse(file: str, data: Iterable[dict[str, Any]]) -> Iterator[tuple[str, str | None, str, str | None]]:
    """(market_hash_name, icon_url, тип, редкость)."""
    for item in data:
        name = item.get("market_hash_name") or item.get("name")
        if name:
            rarity = item.get("rarity")
            yield name, item.get("image"), classify(file, item), rarity.get("name") if isinstance(rarity, dict) else None


def is_stale(conn: sqlite3.Connection, max_age_hours: int = 24) -> bool:
    row = conn.execute("SELECT MAX(updated_at) FROM items").fetchone()
    return not row[0] or datetime.fromisoformat(row[0]) < datetime.now(UTC) - timedelta(hours=max_age_hours)


def refresh(conn: sqlite3.Connection, proxy: str | None = None) -> int:
    """Загружает базу файл за файлом (skins — около 30 МБ), ручные типы не трогает."""
    now = utcnow()
    total = 0
    with httpx.Client(timeout=180, proxy=proxy, follow_redirects=True) as client:
        for file in FILES:
            response = client.get(BASE_URL + file)
            response.raise_for_status()
            rows = list(parse(file, json.loads(response.content)))
            del response
            with conn:
                conn.executemany(
                    """INSERT INTO items (market_hash_name, icon_url, auto_type, rarity, updated_at) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (market_hash_name) DO UPDATE
                       SET icon_url = excluded.icon_url, auto_type = excluded.auto_type, rarity = excluded.rarity,
                           updated_at = excluded.updated_at""",
                    [(name, icon, kind, rarity, now) for name, icon, kind, rarity in rows],
                )
            total += len(rows)
    with conn:
        # Предметы из справочника item_nameid, которых нет в базе ByMykel.
        conn.execute(
            """INSERT INTO items (market_hash_name, icon_url, auto_type, updated_at)
               SELECT market_hash_name, NULL, ?, ? FROM item_nameids WHERE true
               ON CONFLICT (market_hash_name) DO NOTHING""",
            (OTHER, now),
        )
    log.info("Справочник предметов обновлён: %d из базы CS2", total)
    return total


def item_type(row: sqlite3.Row | None) -> str:
    if row is None:
        return OTHER
    return row["type_override"] or row["auto_type"]


def search(conn: sqlite3.Connection, query: str, limit: int = 15) -> list[dict[str, Any]]:
    """Подсказки: сначала совпадения с начала названия, потом по всем словам запроса."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    where = " AND ".join("instr(lower(market_hash_name), ?) > 0" for _ in words)
    rows = conn.execute(
        f"""SELECT market_hash_name, icon_url, auto_type, type_override FROM items WHERE {where}
            ORDER BY instr(lower(market_hash_name), ?) != 1, icon_url IS NULL, length(market_hash_name)
            LIMIT ?""",
        (*words, query.lower().strip(), limit),
    ).fetchall()
    return [{"name": r["market_hash_name"], "icon": r["icon_url"], "type": item_type(r)} for r in rows]


def exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM items WHERE market_hash_name = ?", (name,)).fetchone() is not None


def set_type(conn: sqlite3.Connection, name: str, kind: str | None) -> None:
    if kind is not None and kind not in TYPES:
        raise ValueError(f"неизвестный тип: {kind}")
    with conn:
        conn.execute("UPDATE items SET type_override = ? WHERE market_hash_name = ?", (kind, name))
