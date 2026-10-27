"""Настройки берутся из переменных окружения (на сервере — /etc/steamfolio/steamfolio.env)."""

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"STEAMFOLIO_{name}", default)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    # Пауза между запросами к Steam, секунды.
    request_delay: float
    # Прокси для запросов к Steam (на случай блокировки IP сервера), например http://user:pass@host:port.
    proxy: str | None
    # Открытый справочник item_nameid предметов CS2.
    nameids_url: str
    nameids_max_age_hours: int
    # Предметы для расчёта курса Steam: ликвидные и дороже min_rate_item_usd.
    rate_items: tuple[str, ...]
    min_rate_item_usd: float
    min_rate_samples: int
    # Токен бота — отдельным файлом (не в коде, не в базе и не в бэкапах); задаётся `steamfolio telegram setup`.
    telegram_token_file: Path
    # Ежедневные копии базы на сервере.
    backup_dir: Path
    backup_keep: int
    # Адрес, который проверяет `steamfolio healthcheck`.
    site_url: str


# Не брать предметы дороже ~$1800: у них часто нет лотов (потолок цены площадки).
DEFAULT_RATE_ITEMS = (
    "★ Karambit | Doppler (Factory New)",
    "★ Karambit | Marble Fade (Factory New)",
    "★ M9 Bayonet | Doppler (Factory New)",
    "★ Karambit | Tiger Tooth (Factory New)",
    "★ Talon Knife | Doppler (Factory New)",
)


def load_settings() -> Settings:
    rate_items = _env("RATE_ITEMS", "")
    db_path = Path(_env("DB", "data/steamfolio.db"))
    return Settings(
        db_path=db_path,
        request_delay=float(_env("REQUEST_DELAY", "2.5")),
        proxy=_env("PROXY", "") or None,
        nameids_url=_env(
            "NAMEIDS_URL",
            "https://raw.githubusercontent.com/somespecialone/steam-market-ids/main/data/CS2/item_names.json",
        ),
        nameids_max_age_hours=int(_env("NAMEIDS_MAX_AGE_HOURS", "24")),
        # Названия предметов содержат запятые редко, но «|» — часто, поэтому разделитель «;».
        rate_items=tuple(s.strip() for s in rate_items.split(";") if s.strip()) or DEFAULT_RATE_ITEMS,
        min_rate_item_usd=float(_env("MIN_RATE_ITEM_USD", "500")),
        min_rate_samples=int(_env("MIN_RATE_SAMPLES", "3")),
        telegram_token_file=Path(_env("TELEGRAM_TOKEN_FILE", str(db_path.parent / "telegram_token"))),
        backup_dir=Path(_env("BACKUP_DIR", str(db_path.parent / "backups"))),
        backup_keep=int(_env("BACKUP_KEEP", "14")),
        site_url=f"https://{domain}/login" if (domain := _env("DOMAIN", "")) else "http://127.0.0.1:8000/login",
    )
