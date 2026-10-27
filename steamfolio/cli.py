import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from steamfolio import collector, db
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
    http = SteamHttp(settings.request_delay, settings.proxy)
    try:
        run_id = collector.run(conn, Market(conn, http), settings)
    finally:
        http.close()
    status = conn.execute("SELECT status FROM collector_runs WHERE id = ?", (run_id,)).fetchone()[0]
    # partial — не ошибка запуска: часть цен есть, проблемы видны в report.
    return 1 if status == "failed" else 0


def cmd_runs(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    rows = conn.execute("SELECT * FROM collector_runs ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    print(f"{'#':>4}  {'начало':<22} {'конец':<22} {'статус':<8} {'цены':>7}  курсы")
    for r in rows:
        print(
            f"{r['id']:>4}  {msk(r['started_at']):<22} {msk(r['finished_at']):<22} {r['status']:<8} "
            f"{r['quotes_ok']:>3}/{r['quotes_total']:<3}  {'да' if r['rates_ok'] else 'нет'}"
        )
    last_ok = conn.execute(
        "SELECT MAX(finished_at) FROM collector_runs WHERE status IN ('ok', 'partial')"
    ).fetchone()[0]
    print(f"\nПоследнее успешное обновление: {msk(last_ok)}")
    return 0


def cmd_report(conn: sqlite3.Connection, settings: Settings, args: argparse.Namespace) -> int:
    run = conn.execute(
        "SELECT * FROM collector_runs WHERE (? IS NULL AND status != 'running') OR id = ? ORDER BY id DESC LIMIT 1",
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
