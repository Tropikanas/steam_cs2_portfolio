"""Уведомления после планового сбора: резкие изменения цен и сбои сборщика.

Цены сравниваются по лучшему ордеру в USD, чтобы один предмет в разных валютах не давал два уведомления.
Пороги хранятся в app_settings и меняются на сайте (вкладка «Настройки»).
"""

import html
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from steamfolio import db
from steamfolio.clock import MSK
from steamfolio.currencies import Currency
from steamfolio.ledger import Ledger
from steamfolio.market import listing_url

SETTINGS_KEY = "alerts"
COLLECTOR_ALERT_KEY = "collector_alert"
# Снимок «24 ч / 7 дн назад» ищем в этом окне: сбор раз в 6 часов со случайной задержкой.
SNAPSHOT_WINDOW = timedelta(hours=3)
FAILURES_TO_ALERT = 2

DAY, WEEK2, WEEK3 = "day", "week2", "week3"
RULE_NAMES = {DAY: "Резкое изменение за сутки", WEEK2: "Рост за неделю ×2", WEEK3: "Рост за неделю ×3"}


@dataclass
class AlertSettings:
    enabled: bool = True
    day_pct: float = 50  # рост или падение за сутки, %
    week2_pct: float = 100  # рост за неделю, %
    week3_pct: float = 200
    min_price_usd: float = 0.10  # если цена и тогда, и сейчас ниже — правила не проверяются
    cooldown_hours: int = 24  # не чаще одного уведомления по предмету и правилу

    def validate(self) -> None:
        if not (0 < self.day_pct <= 1000 and 0 < self.week2_pct <= 10000 and 0 < self.week3_pct <= 10000):
            raise ValueError("пороги в процентах должны быть больше нуля")
        if self.week3_pct <= self.week2_pct:
            raise ValueError("порог «×3 за неделю» должен быть больше порога «×2 за неделю»")
        if not 0 <= self.min_price_usd <= 100000:
            raise ValueError("минимальная цена не может быть отрицательной")
        if not 1 <= self.cooldown_hours <= 24 * 30:
            raise ValueError("антиспам — от 1 часа до 30 дней")


def load_settings(conn: sqlite3.Connection) -> AlertSettings:
    stored = db.get_setting(conn, SETTINGS_KEY, {})
    known = {f.name for f in fields(AlertSettings)}
    return AlertSettings(**{k: v for k, v in stored.items() if k in known})


def save_settings(conn: sqlite3.Connection, settings: AlertSettings) -> None:
    settings.validate()
    db.set_setting(conn, SETTINGS_KEY, asdict(settings))


@dataclass
class Alert:
    item: str
    rule: str
    then: int  # центы
    now: int
    quantity: int

    @property
    def pct(self) -> float:
        return float((Decimal(self.now) / Decimal(self.then) - 1) * 100)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _price_near(conn: sqlite3.Connection, item: str, moment: datetime) -> int | None:
    row = conn.execute(
        """SELECT best_buy FROM price_snapshots
           WHERE market_hash_name = ? AND currency = ? AND best_buy > 0 AND taken_at BETWEEN ? AND ?
           ORDER BY abs(julianday(taken_at) - julianday(?)) LIMIT 1""",
        (item, int(Currency.USD), _iso(moment - SNAPSHOT_WINDOW), _iso(moment + SNAPSHOT_WINDOW), _iso(moment)),
    ).fetchone()
    return row[0] if row else None


def _latest_price(conn: sqlite3.Connection, item: str, now: datetime) -> int | None:
    row = conn.execute(
        """SELECT best_buy FROM price_snapshots
           WHERE market_hash_name = ? AND currency = ? AND best_buy > 0 AND taken_at BETWEEN ? AND ?
           ORDER BY taken_at DESC LIMIT 1""",
        (item, int(Currency.USD), _iso(now - SNAPSHOT_WINDOW), _iso(now)),
    ).fetchone()
    return row[0] if row else None


def _recently_sent(conn: sqlite3.Connection, item: str, rules: tuple[str, ...], since: datetime) -> bool:
    marks = ",".join("?" * len(rules))
    return conn.execute(
        f"SELECT 1 FROM alerts_sent WHERE market_hash_name = ? AND rule IN ({marks}) AND sent_at > ? LIMIT 1",
        (item, *rules, _iso(since)),
    ).fetchone() is not None


def check(conn: sqlite3.Connection, ledger: Ledger, now: datetime, settings: AlertSettings) -> list[Alert]:
    quantities: dict[str, int] = {}
    for p in ledger.open_positions:
        if p.quantity:
            quantities[p.key.market_hash_name] = quantities.get(p.key.market_hash_name, 0) + p.quantity
    min_cents = Decimal(str(settings.min_price_usd)) * 100
    since = now - timedelta(hours=settings.cooldown_hours)
    alerts = []
    for item, quantity in sorted(quantities.items()):
        current = _latest_price(conn, item, now)
        if current is None:
            continue
        found: list[str] = []
        day = _price_near(conn, item, now - timedelta(days=1))
        if day and max(day, current) >= min_cents:
            if abs(Alert(item, DAY, day, current, quantity).pct) >= settings.day_pct:
                found.append(DAY)
        week = _price_near(conn, item, now - timedelta(days=7))
        if week and max(week, current) >= min_cents:
            pct = Alert(item, WEEK2, week, current, quantity).pct
            # Сработали и ×2, и ×3 — приходит только ×3.
            if pct >= settings.week3_pct:
                found.append(WEEK3)
            elif pct >= settings.week2_pct:
                found.append(WEEK2)
        for rule in found:
            # ×2 после недавнего ×3 — не новость.
            blocking = (WEEK2, WEEK3) if rule == WEEK2 else (rule,)
            if _recently_sent(conn, item, blocking, since):
                continue
            alerts.append(Alert(item, rule, day if rule == DAY else week, current, quantity))
    order = {WEEK3: 0, WEEK2: 1, DAY: 2}
    alerts.sort(key=lambda a: (order[a.rule], -abs(a.pct)))
    return alerts


def _usd(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}".replace(",", " ")


def format_alerts(alerts: list[Alert], now: datetime) -> str:
    lines = [f"<b>Портфолио Steam</b> · цены на {now.astimezone(MSK):%d.%m %H:%M} МСК", ""]
    for a in alerts:
        link = f'<a href="{html.escape(listing_url(a.item))}">{html.escape(a.item)}</a>'
        lines.append(
            f"{RULE_NAMES[a.rule]} · {link} · {_usd(a.then)} → {_usd(a.now)} · {a.pct:+.0f}% · у вас {a.quantity} шт."
        )
    return "\n".join(lines)


def record(conn: sqlite3.Connection, alerts: list[Alert], now: datetime) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO alerts_sent (market_hash_name, rule, sent_at, price_then, price_now) VALUES (?, ?, ?, ?, ?)",
            [(a.item, a.rule, _iso(now), a.then, a.now) for a in alerts],
        )


def collector_health(conn: sqlite3.Connection) -> tuple[bool, str] | None:
    """(новое состояние «предупреждение отправлено», текст) — если пора написать о сбоях или о восстановлении."""
    runs = conn.execute(
        """SELECT status, error, finished_at FROM collector_runs WHERE kind = 'scheduled' AND status != 'running'
           ORDER BY id DESC LIMIT ?""",
        (FAILURES_TO_ALERT,),
    ).fetchall()
    alerted = db.get_setting(conn, COLLECTOR_ALERT_KEY, False)
    if not runs:
        return None
    if not alerted and len(runs) == FAILURES_TO_ALERT and all(r["status"] == "failed" for r in runs):
        last_ok = conn.execute(
            "SELECT MAX(finished_at) FROM collector_runs WHERE kind = 'scheduled' AND status IN ('ok', 'partial')"
        ).fetchone()[0]
        when = datetime.fromisoformat(last_ok).astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК") if last_ok else "ещё не было"
        error = html.escape((runs[0]["error"] or "").split("\n")[0][:300])
        text = (f"⚠️ <b>Портфолио Steam</b>: цены не обновились {FAILURES_TO_ALERT} раза подряд.\n"
                f"Последнее успешное обновление: {when}.\nОшибка: {error or '—'}\n"
                "Подробности на сервере: <code>steamfolio report</code>")
        return True, text
    if alerted and runs[0]["status"] in ("ok", "partial"):
        return False, "✅ <b>Портфолио Steam</b>: сбор цен снова работает."
    return None


def after_collect(
    conn: sqlite3.Connection, ledger: Ledger, send: Callable[[str], bool], now: datetime | None = None
) -> dict[str, Any]:
    """Проверить правила и состояние сборщика, отправить сообщения. send возвращает False, если Telegram не настроен."""
    now = now or datetime.now(UTC)
    result: dict[str, Any] = {"alerts": 0, "health": None}
    health = collector_health(conn)
    if health is not None:
        state, text = health
        if send(text):
            db.set_setting(conn, COLLECTOR_ALERT_KEY, state)
            result["health"] = "failed" if state else "recovered"
    settings = load_settings(conn)
    if settings.enabled:
        alerts = check(conn, ledger, now, settings)
        if alerts and send(format_alerts(alerts, now)):
            record(conn, alerts, now)
            result["alerts"] = len(alerts)
    return result
