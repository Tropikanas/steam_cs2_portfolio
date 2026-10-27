"""Даты операций — московские календарные даты; время действий хранится в UTC."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


def today_msk() -> date:
    return datetime.now(MSK).date()


def msk_day_end_utc(day: date) -> str:
    """Начало следующих суток по Москве в UTC, в формате taken_at."""
    end = datetime.combine(day + timedelta(days=1), time(0), MSK).astimezone(UTC)
    return end.isoformat(timespec="seconds")


def msk_date(ts: str) -> date:
    return datetime.fromisoformat(ts).astimezone(MSK).date()
