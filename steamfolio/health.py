"""Проверка, что сайт открывается (таймер раз в 15 минут): после 2 неудач подряд — сообщение в Telegram.

Проверка идёт с самого сервера через публичный адрес, то есть охватывает Caddy, сертификат и приложение.
Если выключен весь сервер, сообщить некому — это видно по тому, что перестали приходить уведомления и бэкапы.
"""

import html
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx

from steamfolio import db

STATE_KEY = "site_health"
FAILURES_TO_ALERT = 2


def probe(url: str, timeout: float = 20) -> str | None:
    """None — сайт отвечает; иначе текст ошибки."""
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as e:
        return f"{type(e).__name__}: {e}"
    if response.status_code >= 500:
        return f"сайт ответил кодом {response.status_code}"
    return None


def update(conn: sqlite3.Connection, url: str, error: str | None, send: Callable[[str], bool]) -> dict[str, Any]:
    """Учесть результат проверки; отправить сообщение о сбое или о восстановлении, когда пора."""
    state = db.get_setting(conn, STATE_KEY, {"fails": 0, "alerted": False})
    if error is None:
        if state["alerted"] and send(f"✅ <b>Портфолио Steam</b>: сайт снова открывается.\n{html.escape(url)}"):
            state["alerted"] = False
        state["fails"] = 0
    else:
        state["fails"] += 1
        state["last_error"] = error[:300]
        if state["fails"] >= FAILURES_TO_ALERT and not state["alerted"]:
            text = (f"⚠️ <b>Портфолио Steam</b>: сайт не открывается ({state['fails']} проверки подряд).\n"
                    f"{html.escape(url)}\nОшибка: {html.escape(error[:300])}\n"
                    "На сервере: <code>systemctl status steamfolio-web caddy</code>")
            if send(text):
                state["alerted"] = True
    db.set_setting(conn, STATE_KEY, state)
    return state
