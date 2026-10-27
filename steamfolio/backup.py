"""Резервные копии базы: ежедневно на сервере (храним settings.backup_keep штук) и раз в неделю в Telegram."""

import gzip
import logging
import sqlite3
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from steamfolio import db, telegram
from steamfolio.clock import MSK
from steamfolio.settings import Settings

log = logging.getLogger(__name__)
SENT_KEY = "backup_sent_at"
SEND_WEEKDAY = 0  # понедельник; таймер запускает копирование в 01:00 МСК
PREFIX = "steamfolio-"


def _snapshot(conn: sqlite3.Connection, dest: Path, strip_secrets: bool = False) -> None:
    """Согласованная копия работающей базы (online backup API, WAL учитывается)."""
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
        if strip_secrets:
            # Копия уходит на серверы Telegram: сессии входа и IP из попыток входа там не нужны.
            target.execute("DELETE FROM sessions")
            target.execute("DELETE FROM login_attempts")
            target.commit()
            target.execute("VACUUM")
    finally:
        target.close()


def _gzip(source: Path, dest: Path) -> None:
    with source.open("rb") as src, gzip.open(dest, "wb", compresslevel=9) as out:
        while chunk := src.read(1 << 20):
            out.write(chunk)


def local(conn: sqlite3.Connection, settings: Settings, today: date) -> Path:
    """Копия за день: backups/steamfolio-ГГГГ-ММ-ДД.db.gz; старше последних backup_keep — удаляются."""
    settings.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = settings.backup_dir / f"{PREFIX}{today.isoformat()}.db.gz"
    with tempfile.TemporaryDirectory(dir=settings.backup_dir) as tmp:
        raw = Path(tmp) / "copy.db"
        _snapshot(conn, raw)
        partial = Path(tmp) / dest.name
        _gzip(raw, partial)
        partial.replace(dest)
    dest.chmod(0o600)
    copies = sorted(settings.backup_dir.glob(f"{PREFIX}*.db.gz"))
    for old in copies[: max(0, len(copies) - settings.backup_keep)]:
        old.unlink()
    return dest


def list_local(settings: Settings) -> list[dict[str, Any]]:
    if not settings.backup_dir.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size}
            for p in sorted(settings.backup_dir.glob(f"{PREFIX}*.db.gz"), reverse=True)]


def for_telegram(conn: sqlite3.Connection, settings: Settings) -> bytes:
    settings.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.backup_dir) as tmp:
        raw, packed = Path(tmp) / "copy.db", Path(tmp) / "copy.db.gz"
        _snapshot(conn, raw, strip_secrets=True)
        _gzip(raw, packed)
        return packed.read_bytes()


def send_due(conn: sqlite3.Connection, now: datetime) -> bool:
    """Понедельник по Москве и сегодня ещё не отправляли; либо прошлая отправка пропущена (больше 7 дней)."""
    last = db.get_setting(conn, SENT_KEY)
    today = now.astimezone(MSK).date()
    if last is None:
        return today.weekday() == SEND_WEEKDAY
    last_at = datetime.fromisoformat(last)
    if last_at.astimezone(MSK).date() == today:
        return False
    return today.weekday() == SEND_WEEKDAY or now - last_at > timedelta(days=7)


def send(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> bool:
    """Отправить копию в Telegram; False — Telegram не настроен."""
    now = now or datetime.now(UTC)
    found = telegram.bot_for(conn, settings)
    if found is None:
        log.info("Telegram не настроен — копия не отправлена")
        return False
    bot, chat_id = found
    content = for_telegram(conn, settings)
    stamp = now.astimezone(MSK)
    try:
        bot.send_document(chat_id, f"{PREFIX}{stamp:%Y-%m-%d}.db.gz", content,
                          caption=f"Резервная копия базы «Портфолио Steam» на {stamp:%d.%m.%Y %H:%M} МСК")
    finally:
        bot.close()
    db.set_setting(conn, SENT_KEY, now.isoformat(timespec="seconds"))
    return True
