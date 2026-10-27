import argparse
import getpass
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from steamfolio import alerts, auth, backup, catalog, collector, db, health, history, telegram
from steamfolio.ledger import Op, replay
from steamfolio.currencies import RATE_CURRENCIES, USER_CURRENCIES, Currency, format_minor
from steamfolio.market import Market, SteamHttp, listing_url, nameids
from steamfolio.settings import Settings, load_settings

log = logging.getLogger("steamfolio")
MSK = ZoneInfo("Europe/Moscow")


def msk(ts: str | None) -> str:
    if not ts:
        return "—"
    return datetime.fromisoformat(ts).astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def cmd_collect(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    if args.refresh_nameids or nameids.is_stale(conn, settings.nameids_max_age_hours):
        try:
            nameids.refresh(conn, settings.nameids_url, settings.proxy)
        except Exception as e:  # старый справочник лучше, чем никакого
            log.warning("Не удалось обновить справочник item_nameid: %r", e)
    if catalog.is_stale(conn):
        try:
            catalog.refresh(conn, settings.proxy)
        except Exception as e:
            log.warning("Не удалось обновить справочник предметов: %r", e)
    http = SteamHttp(settings.request_delay, settings.proxy)
    try:
        run_id = collector.run(conn, Market(conn, http), settings)
        ops = [Op.from_row(r) for r in conn.execute("SELECT * FROM operations ORDER BY id")]
        try:
            history.backfill(conn, http, ops)
        except Exception as e:  # оценка прошлого не должна ронять сбор цен
            log.warning("Не удалось загрузить историю цен: %r", e)
    finally:
        http.close()
    try:
        result = alerts.after_collect(conn, replay(ops), lambda text: telegram.notify(conn, settings, text))
        log.info("Уведомления: %s", result)
    except Exception as e:
        log.warning("Не удалось отправить уведомления: %r", e)
    status = conn.execute("SELECT status FROM collector_runs WHERE id = ?", (run_id,)).fetchone()[0]
    # partial — не ошибка запуска: часть цен есть, проблемы видны в report.
    return 1 if status == "failed" else 0


def cmd_runs(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    rows = conn.execute(
        "SELECT * FROM collector_runs WHERE kind = 'scheduled' ORDER BY id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    print(f"{'#':>4}  {'начало':<22} {'конец':<22} {'статус':<8} {'цены':>7}  курсы")
    for r in rows:
        print(
            f"{r['id']:>4}  {msk(r['started_at']):<22} {msk(r['finished_at']):<22} {r['status']:<8} "
            f"{r['quotes_ok']:>3}/{r['quotes_total']:<3}  {'да' if r['rates_ok'] else 'нет'}"
        )
    last_ok = conn.execute(
        "SELECT MAX(finished_at) FROM collector_runs WHERE kind = 'scheduled' AND status IN ('ok', 'partial')"
    ).fetchone()[0]
    print(f"\nПоследнее успешное обновление: {msk(last_ok)}")
    return 0


def cmd_report(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    run = conn.execute(
        """SELECT * FROM collector_runs WHERE (? IS NULL AND status != 'running' AND kind = 'scheduled') OR id = ?
           ORDER BY id DESC LIMIT 1""",
        (args.run, args.run),
    ).fetchone()
    if run is None:
        print("Запусков ещё не было: steamfolio collect")
        return 1
    print(f"Запуск #{run['id']} · {msk(run['started_at'])} → {msk(run['finished_at'])} · {run['status']}\n")

    print("КУРСЫ STEAM (единиц валюты за 1 USD)")
    samples = conn.execute(
        "SELECT market_hash_name, currency, lowest_sell FROM fx_samples WHERE run_id = ? ORDER BY rowid", (run["id"],)
    ).fetchall()
    by_item: dict[str, dict[int, int | None]] = {}
    for s in samples:
        by_item.setdefault(s["market_hash_name"], {})[s["currency"]] = s["lowest_sell"]
    header = f"  {'лот (самый дешёвый на продаже)':<42}" + "".join(f"{c.name:>13}" for c in (Currency.USD, *RATE_CURRENCIES))
    print(header)
    for name, prices in by_item.items():
        cells = "".join(f"{format_minor(prices.get(c)):>13}" for c in (Currency.USD, *RATE_CURRENCIES))
        print(f"  {name[:42]:<42}{cells}")
    rates = conn.execute("SELECT * FROM fx_snapshots WHERE run_id = ?", (run["id"],)).fetchall()
    rate_by_c = {Currency(r["currency"]): r for r in rates}
    line = f"  {'КУРС (медиана)':<42}{'1.000':>13}"
    for c in RATE_CURRENCIES:
        r = rate_by_c.get(c)
        line += f"{r['rate']:>13.3f}" if r else f"{'нет':>13}"
    print(line)
    line = f"  {'разброс между предметами':<42}{'':>13}"
    for c in RATE_CURRENCIES:
        r = rate_by_c.get(c)
        line += f"{r['spread_pct']:>12.2f}%" if r else f"{'':>13}"
    print(line + "\n")

    print("ЛУЧШИЙ ОРДЕР НА ПОКУПКУ")
    quotes = conn.execute(
        "SELECT market_hash_name, currency, best_buy, source FROM price_snapshots WHERE run_id = ? ORDER BY rowid",
        (run["id"],),
    ).fetchall()
    table: dict[str, dict[int, sqlite3.Row]] = {}
    for q in quotes:
        table.setdefault(q["market_hash_name"], {})[q["currency"]] = q
    comments = dict(conn.execute("SELECT market_hash_name, comment FROM tracked_items").fetchall())
    print(f"  {'#':>2} {'предмет':<48}" + "".join(f"{c.name:>12}" for c in USER_CURRENCIES) + "  примечание")
    for i, (name, by_c) in enumerate(table.items(), 1):
        cells = "".join(f"{format_minor(by_c[c]['best_buy']) if c in by_c else 'ошибка':>12}" for c in USER_CURRENCIES)
        notes = [comments.get(name, "")]
        if any(q["source"] != "histogram" for q in by_c.values()):
            notes.append("пересчёт из EUR (нет item_nameid)")
        print(f"  {i:>2} {name[:48]:<48}{cells}  {', '.join(n for n in notes if n)}")
    if args.links:
        print("\nСсылки:")
        for i, name in enumerate(table, 1):
            print(f"  {i:>2} {listing_url(name)}")
    if run["error"]:
        print("\nПРОБЛЕМЫ:\n  " + run["error"].replace("\n", "\n  "))
    return 0


def cmd_track(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "list":
        for row in conn.execute("SELECT * FROM tracked_items ORDER BY rowid"):
            nid = nameids.get_nameid(conn, row["market_hash_name"])
            print(f"{row['market_hash_name']:<55} {row['comment']:<10} item_nameid={nid or 'НЕТ'}")
        return 0
    names = list(args.names)
    if args.file:
        names += [ln.strip() for ln in Path(args.file).read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")]
    with conn:
        for name in names:
            if args.action == "add":
                conn.execute(
                    """INSERT INTO tracked_items (market_hash_name, comment, added_at) VALUES (?, ?, ?)
                       ON CONFLICT (market_hash_name) DO UPDATE SET comment = excluded.comment""",
                    (name, args.comment, db.utcnow()),
                )
            else:
                conn.execute("DELETE FROM tracked_items WHERE market_hash_name = ?", (name,))
    print(f"{'Добавлено' if args.action == 'add' else 'Удалено'}: {len(names)}")
    return 0


def cmd_nameid(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "refresh":
        nameids.refresh(conn, settings.nameids_url, settings.proxy)
    else:
        nameids.set_manual(conn, args.name, args.item_nameid)
    return 0


def cmd_set_password(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    password = getpass.getpass("Новый пароль (от 10 символов): ")
    if password != getpass.getpass("Ещё раз: "):
        print("Пароли не совпадают")
        return 1
    try:
        auth.set_password(conn, args.login, password)
    except ValueError as e:
        print(f"Ошибка: {e}")
        return 1
    print(f"Пароль для {args.login} задан. Все прежние сессии на сайте закрыты.")
    return 0


def cmd_catalog(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "refresh":
        catalog.refresh(conn, settings.proxy)
    elif args.action == "type":
        catalog.set_type(conn, args.name, args.type or None)
    else:
        for item in catalog.search(conn, args.name or ""):
            print(f"{item['type']:<22} {item['name']}")
    return 0


def cmd_telegram(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "status":
        chat = telegram.owner_chat(conn)
        print(f"Токен: {'задан' if telegram.read_token(settings) else 'не задан'} ({settings.telegram_token_file})")
        print(f"Чат владельца: {chat['name'] if chat else 'не привязан'}")
        return 0
    if args.action == "test":
        try:
            sent = telegram.notify(conn, settings, "🔔 Тестовое сообщение «Портфолио Steam»: уведомления работают.")
        except telegram.TelegramError as e:
            print(f"Ошибка Telegram: {e}")
            return 1
        print("Отправлено" if sent else "Telegram не настроен: steamfolio telegram setup")
        return 0 if sent else 1
    return _telegram_setup(conn, settings)


def _telegram_setup(conn: sqlite3.Connection, settings: Settings) -> int:
    token = getpass.getpass("Токен бота от @BotFather (ввод не отображается): ").strip()
    bot = telegram.Bot(token)
    try:
        try:
            me = bot.me()
        except telegram.TelegramError as e:
            print(f"Токен не подошёл: {e}")
            return 1
        # Старые сообщения боту не считаются — ждём новое, написанное после запуска команды.
        pending = bot.updates(timeout=0)
        offset = pending[-1]["update_id"] + 1 if pending else None
        print(f"Бот @{me['username']}. Напишите ему в Telegram любое сообщение — жду 2 минуты…")
        chat = None
        deadline = time.monotonic() + 120
        while chat is None and time.monotonic() < deadline:
            for update in bot.updates(offset, timeout=25):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                if message.get("chat", {}).get("type") == "private":
                    chat = message["chat"]
                    break
        if chat is None:
            print("Сообщение не пришло. Запустите команду ещё раз.")
            return 1
        bot.updates(offset, timeout=0)  # отметить прочитанным
        name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        if chat.get("username"):
            name += f" (@{chat['username']})"
        if input(f"Сообщение от {name}. Это вы? [д/н] ").strip().lower() not in ("д", "да", "y", "yes"):
            print("Отменено.")
            return 1
        telegram.save_token(settings, token)
        db.set_setting(conn, telegram.CHAT_KEY, {"id": chat["id"], "name": name})
        bot.send(chat["id"], "✅ Бот «Портфолио Steam» подключён. Сюда будут приходить уведомления о ценах, "
                             "предупреждения о сбоях и еженедельная резервная копия базы.")
    finally:
        bot.close()
    print(f"Готово: уведомления будут приходить {name}. Токен сохранён в {settings.telegram_token_file}")
    return 0


def cmd_backup(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    path = backup.local(conn, settings, now.astimezone(MSK).date())
    print(f"Копия на сервере: {path}")
    if args.send or (args.scheduled and backup.send_due(conn, now)):
        try:
            sent = backup.send(conn, settings, now)
        except telegram.TelegramError as e:
            print(f"Ошибка Telegram: {e}")
            return 1
        print("Копия отправлена в Telegram" if sent else "Telegram не настроен: steamfolio telegram setup")
        return 0 if sent or not args.send else 1
    return 0


def cmd_healthcheck(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    error = health.probe(settings.site_url)
    state = health.update(conn, settings.site_url, error, lambda text: telegram.notify(conn, settings, text))
    print(f"{settings.site_url}: {'работает' if error is None else 'ОШИБКА ' + error} (неудач подряд: {state['fails']})")
    return 0 if error is None else 1


def cmd_demo(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    from steamfolio import demo

    return demo.run(settings.db_path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="steamfolio", description="Портфолио Steam")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("collect", help="снять цены и курсы (запускается таймером раз в 6 часов)")
    s.add_argument("--refresh-nameids", action="store_true", help="обновить справочник item_nameid принудительно")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("report", help="таблица цен и курсов последнего запуска")
    s.add_argument("--run", type=int, help="номер запуска (по умолчанию последний завершённый)")
    s.add_argument("--links", action="store_true", help="показать ссылки на предметы в Steam")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("runs", help="история запусков сборщика")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser("track", help="отслеживаемые предметы вне портфеля")
    s.add_argument("action", choices=["add", "remove", "list"])
    s.add_argument("names", nargs="*", help="названия как на площадке")
    s.add_argument("--file", help="файл с названиями, по одному в строке")
    s.add_argument("--comment", default="")
    s.set_defaults(func=cmd_track)

    s = sub.add_parser("nameid", help="справочник item_nameid")
    nsub = s.add_subparsers(dest="action", required=True)
    nsub.add_parser("refresh", help="загрузить справочник заново")
    ns = nsub.add_parser("set", help="задать item_nameid вручную")
    ns.add_argument("name")
    ns.add_argument("item_nameid", type=int)
    s.set_defaults(func=cmd_nameid)

    s = sub.add_parser("demo", help="демонстрация журнала и отката на временной базе")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("set-password", help="задать пароль для входа на сайт")
    s.add_argument("--login", default="admin")
    s.set_defaults(func=cmd_set_password)

    s = sub.add_parser("catalog", help="справочник предметов")
    s.add_argument("action", choices=["refresh", "search", "type"])
    s.add_argument("name", nargs="?", help="название или часть названия")
    s.add_argument("type", nargs="?", help=f"для type: один из {', '.join(catalog.TYPES)}; пусто — автоматически")
    s.set_defaults(func=cmd_catalog)

    s = sub.add_parser("telegram", help="бот для уведомлений")
    s.add_argument("action", choices=["setup", "test", "status"],
                   help="setup — ввести токен и привязать ваш чат; test — тестовое сообщение")
    s.set_defaults(func=cmd_telegram)

    s = sub.add_parser("backup", help="резервная копия базы")
    s.add_argument("--send", action="store_true", help="сразу отправить копию в Telegram")
    s.add_argument("--scheduled", action="store_true", help="для таймера: отправить в Telegram, если подошёл срок")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("healthcheck", help="проверить, что сайт открывается (таймер раз в 15 минут)")
    s.set_defaults(func=cmd_healthcheck)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        return args.func(conn, settings, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
