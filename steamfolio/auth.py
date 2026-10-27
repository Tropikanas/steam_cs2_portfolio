"""Вход на сайт: пароли (scrypt), сессии на 30 дней, пауза после 5 неверных попыток."""

import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from steamfolio.db import utcnow

SESSION_DAYS = 30
MAX_FAILURES = 5
LOCKOUT = timedelta(minutes=15)
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    _, salt, digest = stored.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(actual.hex(), digest)


def set_password(conn: sqlite3.Connection, login: str, password: str) -> None:
    if len(password) < 10:
        raise ValueError("пароль короче 10 символов")
    with conn:
        updated = conn.execute(
            "UPDATE users SET password_hash = ? WHERE login = ?", (hash_password(password), login)
        ).rowcount
        if not updated:
            raise ValueError(f"пользователь {login!r} не найден")
        # Новый пароль закрывает все старые сессии.
        conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE login = ?)", (login,))


def locked_for(conn: sqlite3.Connection, login: str, ip: str) -> timedelta | None:
    """Сколько ещё ждать, если за последние 15 минут было 5 неверных попыток (по логину или по IP)."""
    since = (datetime.now(UTC) - LOCKOUT).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT at FROM login_attempts WHERE success = 0 AND at > ? AND (login = ? OR ip = ?)
           ORDER BY at DESC LIMIT ?""",
        (since, login, ip, MAX_FAILURES),
    ).fetchall()
    if len(rows) < MAX_FAILURES:
        return None
    oldest = datetime.fromisoformat(rows[-1][0])
    return max(oldest + LOCKOUT - datetime.now(UTC), timedelta(seconds=1))


def login(conn: sqlite3.Connection, login_name: str, password: str, ip: str) -> int | None:
    """id пользователя или None. Вызывать после проверки locked_for."""
    user = conn.execute("SELECT id, password_hash FROM users WHERE login = ?", (login_name,)).fetchone()
    ok = user is not None and verify_password(password, user["password_hash"])
    with conn:
        conn.execute(
            "INSERT INTO login_attempts (login, ip, at, success) VALUES (?, ?, ?, ?)",
            (login_name, ip, utcnow(), int(ok)),
        )
    return user["id"] if ok else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user_id, now.isoformat(timespec="seconds"),
             (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")),
        )
    return token


def session_user(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute(
        """SELECT u.id, u.login FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ?""",
        (_token_hash(token), utcnow()),
    ).fetchone()


def logout(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        with conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
