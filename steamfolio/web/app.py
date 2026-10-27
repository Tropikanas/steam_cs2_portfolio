"""Веб-интерфейс: страницы и JSON API. Запуск: uvicorn --factory steamfolio.web:create_app."""

# Без `from __future__ import annotations`: FastAPI должен видеть Conn/User, объявленные внутри create_app.
import logging
import sqlite3
import threading
from collections.abc import Iterator
from datetime import date as Date  # поле запроса называется date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from steamfolio import auth, catalog, collector, db, views
from steamfolio.clock import today_msk
from steamfolio.currencies import USER_CURRENCIES, Currency
from steamfolio.fx import FxResolver, FxUnavailable
from steamfolio.journal import Journal, JournalError, create_account
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
        raise ApiError(f"не число: {value!r}") from e
    if amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise ApiError(f"сумма должна быть неотрицательной, не больше двух знаков после запятой: {value}")
    return int(amount * 100)


def rate(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation as e:
        raise ApiError(f"курс не число: {value!r}") from e
    if result <= 0:
        raise ApiError("курс должен быть больше нуля")
    return result


def currency(code: str) -> Currency:
    try:
        cur = Currency[code]
    except KeyError as e:
        raise ApiError(f"неизвестная валюта {code}") from e
    if cur not in USER_CURRENCIES:
        raise ApiError(f"неизвестная валюта {code}")
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


class TransferIn(BaseModel):
    item: str
    from_account_id: int
    to_account_id: int
    currency: str
    quantity: int
    date: Date | None = None
    comment: str = ""


class CommentIn(BaseModel):
    item: str
    account_id: int
    currency: str
    comment: str


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


class AccountIn(BaseModel):
    name: str | None = None
    hidden: bool | None = None


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

    def fetch_prices_now(item: str, cur: Currency) -> None:
        if not on_demand_prices:
            return
        with _fetch_lock:
            conn = db.connect(settings.db_path)
            try:
                target = collector.missing_prices(conn, item, cur)
                if target is None:
                    return
                http = SteamHttp(settings.request_delay, settings.proxy)
                try:
                    collector.run_on_demand(conn, Market(conn, http), [target])
                finally:
                    http.close()
            except Exception:
                log.exception("Не удалось сразу получить цену %r", item)
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

    @app.get("/orders", response_class=HTMLResponse)
    def orders_page(request: Request, user: User):
        return templates.TemplateResponse(request, "orders.html", {"tab": "orders", "user": user})

    # ---------- API: чтение ----------

    @app.get("/api/meta")
    def meta(conn: Conn, user: User) -> dict[str, Any]:
        return {
            "accounts": views.accounts(conn, PORTFOLIO_ID),
            "currencies": [c.name for c in USER_CURRENCIES],
            "today": today_msk().isoformat(),
            "last_update": views.last_update(conn),
        }

    @app.get("/api/portfolio")
    def portfolio_api(conn: Conn, user: User, collapse: bool = False, closed: bool = False) -> dict[str, Any]:
        ledger = journal(conn, user).ledger()
        return views.portfolio(conn, ledger, collapse=collapse, closed=closed, portfolio_id=PORTFOLIO_ID)

    @app.get("/api/orders")
    def orders_api(conn: Conn, user: User) -> dict[str, Any]:
        return views.orders(conn, journal(conn, user).ledger(), PORTFOLIO_ID)

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
        background.add_task(fetch_prices_now, body.item, cur)
        return {"op_id": op_id}

    @app.post("/api/sell")
    def sell(body: TradeIn, conn: Conn, user: User) -> dict[str, Any]:
        op_id = journal(conn, user).sell(body.item, body.account_id, currency(body.currency), body.quantity,
                                         money(body.price), body.date, body.comment.strip(), rate(body.fx_rate))
        return {"op_id": op_id}

    @app.post("/api/transfer")
    def transfer(body: TransferIn, conn: Conn, user: User) -> dict[str, Any]:
        op_id = journal(conn, user).transfer(body.item, body.from_account_id, body.to_account_id,
                                             currency(body.currency), body.quantity, body.date, body.comment.strip())
        return {"op_id": op_id}

    @app.post("/api/position-comment")
    def position_comment(body: CommentIn, conn: Conn, user: User) -> dict[str, Any]:
        cur = currency(body.currency)
        with conn:
            if body.comment.strip():
                conn.execute(
                    """INSERT INTO position_comments (portfolio_id, market_hash_name, account_id, currency, comment)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET comment = excluded.comment""",
                    (PORTFOLIO_ID, body.item, body.account_id, int(cur), body.comment.strip()[:500]),
                )
            else:
                conn.execute(
                    """DELETE FROM position_comments
                       WHERE portfolio_id = ? AND market_hash_name = ? AND account_id = ? AND currency = ?""",
                    (PORTFOLIO_ID, body.item, body.account_id, int(cur)),
                )
        return {"ok": True}

    # ---------- API: ордера ----------

    @app.post("/api/orders")
    def place_order(body: OrderIn, conn: Conn, user: User, background: BackgroundTasks) -> dict[str, Any]:
        check_item(conn, body.item)
        cur = currency(body.currency)
        order_id = journal(conn, user).place_order(body.item, body.account_id, cur, body.quantity,
                                                   money(body.price), body.comment.strip())
        background.add_task(fetch_prices_now, body.item, cur)
        return {"order_id": order_id}

    @app.post("/api/orders/{order_id}/fill")
    def fill_order(order_id: int, body: FillIn, conn: Conn, user: User) -> dict[str, Any]:
        price = money(body.price) if body.price not in (None, "") else None
        op_id = journal(conn, user).fill_order(order_id, body.quantity, body.date, price, rate(body.fx_rate))
        return {"op_id": op_id}

    @app.post("/api/orders/{order_id}/change")
    def change_order(order_id: int, body: ChangeIn, conn: Conn, user: User) -> dict[str, Any]:
        op_id = journal(conn, user).change_order(order_id, money(body.price), body.quantity, body.comment.strip())
        return {"op_id": op_id}

    @app.post("/api/orders/{order_id}/remove")
    def remove_order(order_id: int, conn: Conn, user: User) -> dict[str, Any]:
        return {"op_id": journal(conn, user).remove_order(order_id)}

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
                        raise ApiError("пустое название аккаунта")
                    conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))
                if body.hidden is not None:
                    conn.execute("UPDATE accounts SET hidden = ? WHERE id = ?", (int(body.hidden), account_id))
        except sqlite3.IntegrityError as e:
            raise ApiError("аккаунт с таким названием уже есть") from e
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/delete")
    def delete_account(account_id: int, conn: Conn, user: User) -> dict[str, Any]:
        _own_account(conn, account_id)
        used = conn.execute(
            "SELECT 1 FROM operations WHERE account_id = ? OR to_account_id = ? LIMIT 1", (account_id, account_id)
        ).fetchone()
        if used:
            raise ApiError("по аккаунту есть операции — его можно только скрыть")
        with conn:
            conn.execute("DELETE FROM position_comments WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return {"ok": True}

    def _own_account(conn: sqlite3.Connection, account_id: int) -> None:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE id = ? AND portfolio_id = ?", (account_id, PORTFOLIO_ID)
        ).fetchone()
        if row is None:
            raise ApiError("аккаунт не найден", status=404)

    return app
