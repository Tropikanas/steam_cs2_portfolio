"""Telegram-бот: только исходящие сообщения единственному владельцу.

Токен лежит отдельным файлом (settings.telegram_token_file, права 600), чат владельца — в app_settings.
Команды бота можно добавить позже поверх Bot.updates(), не меняя отправку.
"""

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from steamfolio import db
from steamfolio.settings import Settings

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
MESSAGE_LIMIT = 4000  # у Telegram 4096, оставляем запас на разметку
CHAT_KEY = "telegram_chat"


class _HideTelegramUrls(logging.Filter):
    """httpx пишет в лог адрес запроса, а в адресе Bot API есть токен."""

    def filter(self, record: logging.LogRecord) -> bool:
        return API not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideTelegramUrls())


class TelegramError(Exception):
    pass


class Bot:
    def __init__(self, token: str, timeout: float = 60):
        self._token = token
        self._client = httpx.Client(base_url=f"{API}/bot{token}/", timeout=timeout)

    def call(self, method: str, files: dict[str, Any] | None = None, **params: Any) -> Any:
        try:
            response = self._client.post(method, data=params if files else None, json=None if files else params,
                                         files=files)
            body = response.json()
        except (httpx.HTTPError, ValueError) as e:
            # В тексте ошибки httpx есть адрес с токеном — не выпускаем его наружу.
            raise TelegramError(f"{method}: {type(e).__name__}: {str(e).replace(self._token, '***')}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', response.status_code)}")
        return body["result"]

    def me(self) -> dict[str, Any]:
        return self.call("getMe")

    def send(self, chat_id: int, text: str) -> None:
        for chunk in split(text):
            self.call("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML", disable_web_page_preview=True)

    def send_document(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
        self.call("sendDocument", files={"document": (filename, content)}, chat_id=str(chat_id), caption=caption)

    def updates(self, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        return self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])

    def close(self) -> None:
        self._client.close()


def split(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Делит по строкам, чтобы не разрезать ссылку посередине."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    chunks.append(current)
    return chunks


def read_token(settings: Settings) -> str | None:
    try:
        return settings.telegram_token_file.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def save_token(settings: Settings, token: str) -> None:
    path: Path = settings.telegram_token_file
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token.strip())
    os.chmod(path, 0o600)


def owner_chat(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return db.get_setting(conn, CHAT_KEY)


def bot_for(conn: sqlite3.Connection, settings: Settings) -> tuple[Bot, int] | None:
    """Бот и чат владельца, если Telegram настроен."""
    token, chat = read_token(settings), owner_chat(conn)
    if not token or not chat:
        return None
    return Bot(token), int(chat["id"])


def notify(conn: sqlite3.Connection, settings: Settings, text: str) -> bool:
    """Отправить владельцу; False — Telegram не настроен."""
    found = bot_for(conn, settings)
    if found is None:
        log.info("Telegram не настроен — сообщение не отправлено")
        return False
    bot, chat_id = found
    try:
        bot.send(chat_id, text)
    finally:
        bot.close()
    return True
