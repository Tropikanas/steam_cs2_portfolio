"""Веб-интерфейс: страницы и JSON API. Запуск: uvicorn --factory steamfolio.web:create_app."""

# Без `from __future__ import annotations`: FastAPI должен видеть Conn/User, объявленные внутри create_app.
import logging
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import asdict
from datetime import date as Date  # поле запроса называется date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from steamfolio import alerts, auth, backup, catalog, collector, db, history, stats, telegram, views
from steamfolio.clock import today_msk
from steamfolio.currencies import NAMES, SYMBOLS, USER_CURRENCIES, Currency
from steamfolio.fx import FxResolver, FxUnavailable
from steamfolio.journal import Journal, JournalError, create_account
from steamfolio.ledger import Op, replay
from steamfolio.market import Market, SteamHttp
from steamfolio.settings import Settings, load_settings

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
COOKIE = "sf_session"
PORTFOLIO_ID = 1
# Цены новых предметов запрашиваются по одному, чтобы не упереться в лимиты Steam.
_fetch_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, message: str, violations: list[str] | None = None, status: int = 400):
        self.message, self.violations, self.status = message, violations or [], status


def money(value: str | float | int) -> int:
    """«9,45» → 945 (сотые доли)."""
    try:
        amount = Decimal(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation as e:
        raise ApiError(f"Не число: {value!r}") from e
    if amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ApiError(f"Сумма должна быть неотрицательной, не больше двух знаков после запятой: {value}")
    return int(amount * 100)


def rate(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation as e:
        raise ApiError(f"Курс не число: {value!r}") from e
    if result <= 0:
        raise ApiError("Курс должен быть больше нуля")
    return result


def currency(code: str) -> Currency:
    try:
        cur = Currency[code]
    except KeyError as e:
        raise ApiError(f"Неизвестная валюта {code}") from e
    if cur not in USER_CURRENCIES:
        raise ApiError(f"Неизвестная валюта {code}")
    return cur


# ---------- тела запросов ----------

class TradeIn(BaseModel):
    item: str
    account_id: int
    currency: str
    quantity: int
    price: str
    date: Date | None = None
    comment: str = ""
    fx_rate: str | None = None


class SellIn(BaseModel):
    lot_id: int  # строка портфеля (её покупка), из которой продано
    quantity: int
    price: str
    date: Date | None = None
    comment: str = ""
    fx_rate: str | None = None


class TransferIn(BaseModel):
    lot_id: int
    to_account_id: int
    quantity: int
    comment: str = ""


class LotIn(BaseModel):
    lot_id: int


class OrderIn(BaseModel):
    item: str
    account_id: int
    currency: str
    quantity: int
    price: str
    comment: str = ""


class FillIn(BaseModel):
    quantity: int | None = None
    date: Date | None = None
    price: str | None = None
    fx_rate: str | None = None


class ChangeIn(BaseModel):
    price: str
    quantity: int
    comment: str = ""
    account_id: int | None = None
    currency: str | None = None


class EditIn(BaseModel):
    date: Date | None = None
    item: str | None = None
    account_id: int | None = None
    currency: str | None = None
    quantity: int | None = None
    price: str | None = None
    fx_rate: str | None = None
    comment: str | None = None


class AccountIn(BaseModel):
    name: str | None = None
    hidden: bool | None = None


class AlertsIn(BaseModel):
    enabled: bool
    day_pct: float
    week2_pct: float
    week3_pct: float
    min_price_usd: float
    cooldown_hours: int


def create_app(settings: Settings | None = None, cbr_fetch=None, on_demand_prices: bool = True) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Портфолио Steam", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    db.connect(settings.db_path).close()  # миграции при старте

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def current_user(request: Request, conn: Conn) -> sqlite3.Row:
        user = auth.session_user(conn, request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(401, "нужно войти")
        return user

    User = Annotated[sqlite3.Row, Depends(current_user)]

    def journal(conn: sqlite3.Connection, user: sqlite3.Row) -> Journal:
        fx = FxResolver(conn) if cbr_fetch is None else FxResolver(conn, cbr_fetch)
        return Journal(conn, fx, PORTFOLIO_ID, user["id"])

    def fetch_prices_now() -> None:
        """Сразу после действия: цены, которых нет или которые устарели (новый предмет, вернувшаяся позиция)."""
        if not on_demand_prices:
            return
        with _fetch_lock:
            conn = db.connect(settings.db_path)
            try:
                wanted = collector.stale_targets(conn)
                http = SteamHttp(settings.request_delay, settings.proxy)
                try:
                    if wanted:
                        collector.run_on_demand(conn, Market(conn, http), wanted)
                    # Покупка задним числом — сразу подтягиваем историю для оценки стоимости в прошлом.
                    ops = [Op.from_row(r) for r in conn.execute("SELECT * FROM operations ORDER BY id")]
                    history.backfill(conn, http, ops, limit=3)
                finally:
                    http.close()
            except Exception:
                log.exception("Не удалось сразу получить цены")
            finally:
                conn.close()

    def check_item(conn: sqlite3.Connection, item: str) -> None:
        if not catalog.exists(conn, item):
            raise ApiError(f"«{item}» нет в справочнике предметов — выберите из подсказок")

    @app.exception_handler(ApiError)
    def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.message, "violations": exc.violations}, status_code=exc.status)

    @app.exception_handler(JournalError)
    def journal_error(request: Request, exc: JournalError) -> JSONResponse:
        message = str(exc).split(":\n")[0]
        return JSONResponse({"error": message, "violations": exc.violations}, status_code=400)

    @app.exception_handler(FxUnavailable)
    def fx_error(request: Request, exc: FxUnavailable) -> JSONResponse:
        return JSONResponse({"error": f"{exc} — укажите курс вручную", "violations": []}, status_code=400)

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401 and not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": exc.detail, "violations": []}, status_code=exc.status_code)

    # ---------- вход ----------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login_submit(request: Request, conn: Conn, login: Annotated[str, Form()], password: Annotated[str, Form()]):
        ip = request.client.host if request.client else "?"
        wait = auth.locked_for(conn, login, ip)
        if wait:
            minutes = max(1, round(wait.total_seconds() / 60))
            error = f"Слишком много неверных попыток. Попробуйте через {minutes} мин."
            return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=429)
        user_id = auth.login(conn, login, password, ip)
        if user_id is None:
            return templates.TemplateResponse(request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401)
        response = RedirectResponse("/portfolio", status_code=303)
        response.set_cookie(COOKIE, auth.create_session(conn, user_id), max_age=auth.SESSION_DAYS * 86400,
                            httponly=True, secure=request.url.scheme == "https", samesite="lax")
        return response

    @app.post("/logout")
    def logout(request: Request, conn: Conn):
        auth.logout(conn, request.cookies.get(COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    # ---------- страницы ----------

    @app.get("/")
    def index(user: User):
        return RedirectResponse("/portfolio", status_code=303)

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio_page(request: Request, user: User):
        return templates.TemplateResponse(request, "portfolio.html", {"tab": "portfolio", "user": user})

    @app.get("/orders")
    def orders_page(user: User):
        # Ордера — вкладка страницы «Портфолио».
        return RedirectResponse("/portfolio#orders", status_code=303)

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request, user: User):
        return templates.TemplateResponse(request, "stats.html", {"tab": "stats", "user": user})

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, user: User):
        return templates.TemplateResponse(request, "settings.html", {"tab": "settings", "user": user})

    # ---------- API: чтение ----------

    @app.get("/api/meta")
    def meta(conn: Conn, user: User) -> dict[str, Any]:
        return {
            "accounts": views.accounts(conn, PORTFOLIO_ID),
            "currencies": [{"code": c.name, "symbol": SYMBOLS[c], "name": NAMES[c]} for c in USER_CURRENCIES],
            "today": today_msk().isoformat(),
            "last_update": views.last_update(conn),
        }

    @app.get("/api/portfolio")
    def portfolio_api(conn: Conn, user: User, collapse: bool = False, closed: bool = False) -> dict[str, Any]:
        ops = journal(conn, user).ops()
        return views.portfolio(conn, replay(ops), collapse=collapse, closed=closed, portfolio_id=PORTFOLIO_ID, ops=ops)

    @app.get("/api/orders")
    def orders_api(conn: Conn, user: User) -> dict[str, Any]:
        return views.orders(conn, journal(conn, user).orders(), PORTFOLIO_ID)

    @app.get("/api/rates")
    def rates_api(conn: Conn, user: User) -> dict[str, Any]:
        return {**views.steam_rates(conn), "min_item_usd": settings.min_rate_item_usd}

    @app.get("/api/items")
    def items_api(conn: Conn, user: User, q: str = "") -> list[dict[str, Any]]:
        return catalog.search(conn, q[:100])

    @app.get("/api/fx")
    def fx_api(conn: Conn, user: User, currency: str, on: Date | None = None) -> dict[str, Any]:
        value, source = journal(conn, user).fx.rate(_cur(currency), on or today_msk())
        return {"rate": float(value), "source": source}

    def _cur(code: str) -> Currency:
        return currency(code)

    # ---------- API: портфель ----------

    @app.post("/api/buy")
    def buy(body: TradeIn, conn: Conn, user: User, background: BackgroundTasks) -> dict[str, Any]:
        check_item(conn, body.item)
        cur = currency(body.currency)
        op_id = journal(conn, user).buy(body.item, body.account_id, cur, body.quantity, money(body.price),
                                        body.date, body.comment.strip(), rate(body.fx_rate))
        background.add_task(fetch_prices_now)
        return {"op_id": op_id}

    @app.post("/api/sell")
    def sell(body: SellIn, conn: Conn, user: User) -> dict[str, Any]:
        op_id = journal(conn, user).sell(body.lot_id, body.quantity, money(body.price), body.date,
                                         body.comment.strip(), rate(body.fx_rate))
        return {"op_id": op_id}

    @app.post("/api/transfer")
    def transfer(body: TransferIn, conn: Conn, user: User) -> dict[str, Any]:
        # Комментарий новой строки — из формы (по умолчанию — комментарий исходной строки).
        op_id = journal(conn, user).transfer(body.lot_id, body.to_account_id, body.quantity, body.comment.strip()[:500])
        return {"op_id": op_id}

    @app.post("/api/remove-position")
    def remove_position(body: LotIn, conn: Conn, user: User) -> dict[str, Any]:
        return {"removed": journal(conn, user).remove_position(body.lot_id)}

    # ---------- API: ордера ----------

    @app.post("/api/orders")
    def place_order(body: OrderIn, conn: Conn, user: User, background: BackgroundTasks) -> dict[str, Any]:
        check_item(conn, body.item)
        cur = currency(body.currency)
        order_id = journal(conn, user).place_order(body.item, body.account_id, cur, body.quantity,
                                                   money(body.price), body.comment.strip())
        background.add_task(fetch_prices_now)
        return {"order_id": order_id}

    @app.post("/api/orders/{order_id}/fill")
    def fill_order(order_id: int, body: FillIn, conn: Conn, user: User, background: BackgroundTasks) -> dict[str, Any]:
        price = money(body.price) if body.price not in (None, "") else None
        op_id = journal(conn, user).fill_order(order_id, body.quantity, body.date, price, rate(body.fx_rate))
        # Исполнение задним числом — история продаж для прошлых точек графика сразу, не ждать сбора цен.
        background.add_task(fetch_prices_now)
        return {"op_id": op_id}

    @app.post("/api/orders/{order_id}/change")
    def change_order(order_id: int, body: ChangeIn, conn: Conn, user: User, background: BackgroundTasks) -> dict[str, Any]:
        op_id = journal(conn, user).change_order(
            order_id, money(body.price), body.quantity, body.comment.strip(), account_id=body.account_id,
            currency=currency(body.currency) if body.currency else None)
        background.add_task(fetch_prices_now)  # новая валюта ордера — её цены сразу
        return {"op_id": op_id}

    @app.post("/api/orders/{order_id}/remove")
    def remove_order(order_id: int, conn: Conn, user: User) -> dict[str, Any]:
        return {"op_id": journal(conn, user).remove_order(order_id)}

    # ---------- API: статистика ----------

    @app.get("/api/stats")
    def stats_api(conn: Conn, user: User) -> dict[str, Any]:
        j = journal(conn, user)
        ops = j.ops()
        ledger = replay(ops)
        return {
            "summary": stats.summary(conn, ledger, PORTFOLIO_ID),
            "series": stats.series(conn, ops),
            "chart_currencies": stats.chart_currencies(ledger),
            "types": stats.types(conn, ledger, PORTFOLIO_ID),
            "sales": stats.best_operations(ledger, ops),
        }

    @app.get("/api/stats/series")
    def stats_series(conn: Conn, user: User, currency: str = "TOTAL") -> dict[str, Any]:
        cur = None if currency.upper() == "TOTAL" else _cur(currency.upper())
        return stats.series(conn, journal(conn, user).ops(), currency=cur)

    # ---------- API: правка и удаление покупок и продаж ----------
    # Изменение и удаление могут вернуть позицию в портфель или поменять дату — цены и историю подтягиваем сразу.

    @app.post("/api/ops/{op_id}/edit")
    def op_edit(op_id: int, body: EditIn, conn: Conn, user: User, background: BackgroundTasks) -> dict[str, Any]:
        background.add_task(fetch_prices_now)
        j = journal(conn, user)
        current = j.op(op_id)
        changes: dict[str, Any] = {}
        if body.date is not None and body.date != current.op_date:
            changes["op_date"] = body.date
        if body.item is not None and body.item != current.market_hash_name:
            check_item(conn, body.item)
            changes["market_hash_name"] = body.item
        if body.account_id is not None and body.account_id != current.account_id:
            changes["account_id"] = body.account_id
        if body.currency is not None and currency(body.currency) != current.currency:
            changes["currency"] = currency(body.currency)
        if body.quantity is not None and body.quantity != current.quantity:
            changes["quantity"] = body.quantity
        if body.price not in (None, "") and money(body.price) != current.price:
            changes["price"] = money(body.price)
        manual = rate(body.fx_rate)
        if manual is not None and manual != current.fx_rate:
            changes["fx_rate"] = manual
        if body.comment is not None and body.comment.strip() != current.comment:
            changes["comment"] = body.comment.strip()
        if not changes:
            raise ApiError("Ничего не изменилось")
        return {"op_id": j.edit(op_id, **changes)}

    @app.post("/api/ops/{op_id}/delete")
    def op_delete(op_id: int, conn: Conn, user: User, background: BackgroundTasks) -> dict[str, Any]:
        op_id = journal(conn, user).delete(op_id)
        background.add_task(fetch_prices_now)
        return {"op_id": op_id}

    # ---------- API: настройки ----------

    @app.get("/api/settings")
    def settings_api(conn: Conn, user: User) -> dict[str, Any]:
        chat = telegram.owner_chat(conn)
        return {
            "alerts": asdict(alerts.load_settings(conn)),
            "defaults": asdict(alerts.AlertSettings()),
            "telegram": {"token": telegram.read_token(settings) is not None, "chat": chat["name"] if chat else None},
            "backups": {"local": backup.list_local(settings), "keep": settings.backup_keep,
                        "sent_at": db.get_setting(conn, backup.SENT_KEY)},
        }

    @app.post("/api/settings/alerts")
    def save_alerts(body: AlertsIn, conn: Conn, user: User) -> dict[str, Any]:
        new = alerts.AlertSettings(**body.model_dump())
        try:
            alerts.save_settings(conn, new)
        except ValueError as e:
            raise ApiError(str(e)) from e
        return asdict(new)

    @app.post("/api/settings/telegram-test")
    def telegram_test(conn: Conn, user: User) -> dict[str, Any]:
        try:
            sent = telegram.notify(conn, settings, "🔔 Тестовое сообщение с сайта «Портфолио Steam»: уведомления работают.")
        except telegram.TelegramError as e:
            raise ApiError(f"Telegram ответил ошибкой: {e}") from e
        if not sent:
            raise ApiError("Бот не подключён: на сервере выполните steamfolio telegram setup")
        return {"ok": True}

    @app.post("/api/settings/backup-send")
    def backup_send(conn: Conn, user: User) -> dict[str, Any]:
        try:
            sent = backup.send(conn, settings)
        except telegram.TelegramError as e:
            raise ApiError(f"Telegram ответил ошибкой: {e}") from e
        if not sent:
            raise ApiError("Бот не подключён: на сервере выполните steamfolio telegram setup")
        return {"ok": True}

    # ---------- API: аккаунты ----------

    @app.post("/api/accounts")
    def add_account(body: AccountIn, conn: Conn, user: User) -> dict[str, Any]:
        return {"id": create_account(conn, (body.name or "")[:60], PORTFOLIO_ID)}

    @app.post("/api/accounts/{account_id}")
    def update_account(account_id: int, body: AccountIn, conn: Conn, user: User) -> dict[str, Any]:
        _own_account(conn, account_id)
        try:
            with conn:
                if body.name is not None:
                    name = body.name.strip()[:60]
                    if not name:
                        raise ApiError("Пустое название аккаунта")
                    conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))
                if body.hidden is not None:
                    conn.execute("UPDATE accounts SET hidden = ? WHERE id = ?", (int(body.hidden), account_id))
        except sqlite3.IntegrityError as e:
            raise ApiError("Аккаунт с таким названием уже есть") from e
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/delete")
    def delete_account(account_id: int, conn: Conn, user: User) -> dict[str, Any]:
        _own_account(conn, account_id)
        used = conn.execute(
            """SELECT 1 FROM operations WHERE account_id = ? OR to_account_id = ?
               UNION SELECT 1 FROM orders WHERE account_id = ? LIMIT 1""", (account_id, account_id, account_id)
        ).fetchone()
        if used:
            raise ApiError("По аккаунту есть операции — его можно только скрыть")
        with conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return {"ok": True}

    def _own_account(conn: sqlite3.Connection, account_id: int) -> None:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE id = ? AND portfolio_id = ?", (account_id, PORTFOLIO_ID)
        ).fetchone()
        if row is None:
            raise ApiError("Аккаунт не найден", status=404)

    return app
