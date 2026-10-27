import gzip
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from steamfolio import alerts, backup, db, telegram
from steamfolio.currencies import Currency
from steamfolio.ledger import replay
from steamfolio.settings import load_settings

USD = Currency.USD
CASE, AK = "Kilowatt Case", "AK-47 | Redline (Field-Tested)"
NOW = datetime(2026, 10, 27, 18, 3, tzinfo=UTC)
D = date.fromisoformat


def snapshot(conn, item, moment, cents, status="ok"):
    with conn:
        run = conn.execute(
            "INSERT INTO collector_runs (started_at, finished_at, status) VALUES (?, ?, ?)",
            (moment.isoformat(timespec="seconds"),) * 2 + (status,),
        ).lastrowid
        conn.execute(
            """INSERT INTO price_snapshots (run_id, taken_at, market_hash_name, currency, best_buy, lowest_sell, source)
               VALUES (?, ?, ?, ?, ?, NULL, 'histogram')""",
            (run, moment.isoformat(timespec="seconds"), item, int(USD), cents),
        )


def prices(conn, item, week, day, now):
    snapshot(conn, item, NOW - timedelta(days=7, hours=1), week)
    snapshot(conn, item, NOW - timedelta(days=1, minutes=-40), day)
    snapshot(conn, item, NOW - timedelta(minutes=3), now)


@pytest.fixture
def held(journal, acc):
    journal.buy(CASE, acc, USD, 150, 10, D("2026-10-01"))
    journal.buy(AK, acc, USD, 1, 1000, D("2026-10-01"))
    return lambda: replay(journal.ops())


def test_rules_and_week3_suppresses_week2(conn, held):
    prices(conn, CASE, 12, 20, 38)  # неделя +217%, сутки +90%
    prices(conn, AK, 1000, 1000, 1400)  # +40% — ниже порогов
    found = alerts.check(conn, held(), NOW, alerts.AlertSettings())
    assert [(a.item, a.rule) for a in found] == [(CASE, alerts.WEEK3), (CASE, alerts.DAY)]
    text = alerts.format_alerts(found, NOW)
    assert "Рост за неделю ×3 · <a href=" in text
    assert "Kilowatt Case</a> · $0.12 → $0.38 · +217% · у вас 150 шт." in text
    assert "21:03 МСК" in text


def test_drop_and_min_price(conn, held):
    prices(conn, CASE, 8, 8, 3)  # −62% за сутки, но цена и тогда, и сейчас ниже $0.10
    prices(conn, AK, 1000, 1000, 400)  # −60%
    found = alerts.check(conn, held(), NOW, alerts.AlertSettings())
    assert [(a.item, a.rule, round(a.pct)) for a in found] == [(AK, alerts.DAY, -60)]
    lower = alerts.AlertSettings(min_price_usd=0.05)
    assert {a.item for a in alerts.check(conn, held(), NOW, lower)} == {CASE, AK}


def test_cooldown(conn, held):
    prices(conn, CASE, 12, 12, 30)  # ×2 за неделю и +150% за сутки
    settings = alerts.AlertSettings()
    first = alerts.check(conn, held(), NOW, settings)
    assert [a.rule for a in first] == [alerts.WEEK2, alerts.DAY]
    alerts.record(conn, first, NOW - timedelta(hours=6))
    assert alerts.check(conn, held(), NOW, settings) == []
    assert len(alerts.check(conn, held(), NOW, replace(settings, cooldown_hours=5))) == 2


def test_week2_blocked_after_recent_week3(conn, held):
    prices(conn, CASE, 10, 22, 25)  # +150% за неделю, +14% за сутки
    alerts.record(conn, [alerts.Alert(CASE, alerts.WEEK3, 10, 35, 150)], NOW - timedelta(hours=6))
    assert alerts.check(conn, held(), NOW, alerts.AlertSettings()) == []


def test_no_alert_without_old_snapshot_or_sold_out(conn, journal, acc, held):
    snapshot(conn, CASE, NOW - timedelta(days=3), 10)  # ни 24 ч, ни 7 дн назад снимка нет
    snapshot(conn, CASE, NOW, 40)
    assert alerts.check(conn, held(), NOW, alerts.AlertSettings()) == []
    prices(conn, AK, 1000, 1000, 400)
    journal.sell(AK, acc, USD, 1, 900, D("2026-10-20"))
    assert alerts.check(conn, held(), NOW, alerts.AlertSettings()) == []


def test_settings_roundtrip_and_validation(conn):
    assert alerts.load_settings(conn) == alerts.AlertSettings()
    alerts.save_settings(conn, alerts.AlertSettings(min_price_usd=0.5, day_pct=30))
    assert alerts.load_settings(conn).min_price_usd == 0.5
    with pytest.raises(ValueError):
        alerts.save_settings(conn, alerts.AlertSettings(week2_pct=200, week3_pct=150))


def test_after_collect_sends_once_and_collector_health(conn, held):
    prices(conn, CASE, 12, 20, 38)
    sent: list[str] = []
    result = alerts.after_collect(conn, held(), lambda t: sent.append(t) or True, NOW)
    assert result["alerts"] == 2 and len(sent) == 1
    assert alerts.after_collect(conn, held(), lambda t: sent.append(t) or True, NOW)["alerts"] == 0

    snapshot(conn, AK, NOW + timedelta(hours=6), None, status="failed")
    assert alerts.collector_health(conn) is None  # одна неудача — молчим
    snapshot(conn, AK, NOW + timedelta(hours=12), None, status="failed")
    state, text = alerts.collector_health(conn)
    assert state is True and "2 раза подряд" in text
    alerts.after_collect(conn, held(), lambda t: sent.append(t) or True, NOW + timedelta(hours=12))
    assert alerts.collector_health(conn) is None  # уже предупредили
    snapshot(conn, AK, NOW + timedelta(hours=18), 1000)
    assert alerts.collector_health(conn) == (False, "✅ <b>Портфолио Steam</b>: сбор цен снова работает.")


def test_not_configured_does_not_record(conn, held):
    prices(conn, CASE, 12, 20, 38)
    alerts.after_collect(conn, held(), lambda t: False, NOW)
    assert conn.execute("SELECT count(*) FROM alerts_sent").fetchone()[0] == 0


def test_split_long_message():
    lines = [f"строка {i} " + "x" * 90 for i in range(100)]
    chunks = telegram.split("\n".join(lines), limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines


@pytest.fixture
def file_db(tmp_path):
    settings = replace(load_settings(), db_path=tmp_path / "sf.db", backup_dir=tmp_path / "backups", backup_keep=3,
                       telegram_token_file=tmp_path / "telegram_token")
    conn = db.connect(settings.db_path)
    yield conn, settings
    conn.close()


def test_local_backups_rotate(file_db):
    conn, settings = file_db
    for day in range(1, 6):
        backup.local(conn, settings, date(2026, 10, day))
    names = [b["name"] for b in backup.list_local(settings)]
    assert names == ["steamfolio-2026-10-05.db.gz", "steamfolio-2026-10-04.db.gz", "steamfolio-2026-10-03.db.gz"]


def test_telegram_copy_has_no_sessions(file_db, tmp_path):
    conn, settings = file_db
    with conn:
        conn.execute("INSERT INTO sessions VALUES ('h', 1, '', '2099-01-01')")
        conn.execute("INSERT INTO login_attempts (login, ip, at, success) VALUES ('admin', '1.2.3.4', '', 1)")
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(backup.for_telegram(conn, settings)))
    copy = sqlite3.connect(restored)
    assert copy.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    assert copy.execute("SELECT count(*) FROM login_attempts").fetchone()[0] == 0
    assert copy.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    copy.close()


def test_send_due(conn):
    monday_night = datetime(2026, 11, 1, 22, 0, tzinfo=UTC)  # 02.11 01:00 МСК, понедельник
    assert backup.send_due(conn, monday_night)
    assert not backup.send_due(conn, monday_night - timedelta(days=1))
    db.set_setting(conn, backup.SENT_KEY, monday_night.isoformat())
    assert not backup.send_due(conn, monday_night + timedelta(hours=1))
    assert not backup.send_due(conn, monday_night + timedelta(days=3))
    assert backup.send_due(conn, monday_night + timedelta(days=7))
    # понедельник пропущен (сервер был выключен) — отправляем при первой возможности
    assert backup.send_due(conn, monday_night + timedelta(days=8))


def test_token_file_is_private(file_db):
    conn, settings = file_db
    assert telegram.read_token(settings) is None
    telegram.save_token(settings, " 123:abc \n")
    assert telegram.read_token(settings) == "123:abc"
    assert telegram.bot_for(conn, settings) is None  # чат ещё не привязан
