from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from steamfolio import auth, db
from steamfolio.currencies import Currency
from steamfolio.settings import load_settings
from steamfolio.web import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def client(tmp_path):
    settings = replace(load_settings(), db_path=tmp_path / "web.db", backup_dir=tmp_path / "backups",
                       telegram_token_file=tmp_path / "telegram_token")
    conn = db.connect(settings.db_path)
    auth.set_password(conn, "admin", PASSWORD)
    with conn:
        conn.execute("INSERT INTO items (market_hash_name, icon_url, auto_type, updated_at) VALUES ('Kilowatt Case', NULL, 'Кейсы', '')")
    conn.close()
    app = create_app(settings, cbr_fetch=lambda day: {Currency.RUB: Decimal(80), Currency.CNY: Decimal(7),
                                                      Currency.UAH: Decimal(40)}, on_demand_prices=False)
    return TestClient(app)


def login(client, password=PASSWORD):
    return client.post("/login", data={"login": "admin", "password": password}, follow_redirects=False)


def test_pages_require_login(client):
    assert client.get("/portfolio", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/portfolio").status_code == 401


def test_login_and_lockout(client):
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    assert login(client).status_code == 429  # пауза даже для верного пароля
    assert client.get("/api/portfolio").status_code == 401


def test_buy_transfer_and_order_flow(client):
    assert login(client).status_code == 303
    acc1 = client.post("/api/accounts", json={"name": "Основной"}).json()["id"]
    acc2 = client.post("/api/accounts", json={"name": "Твинк"}).json()["id"]

    bad = client.post("/api/buy", json={"item": "Нет такого", "account_id": acc1, "currency": "RUB", "quantity": 1, "price": "1"})
    assert bad.status_code == 400 and "справочнике" in bad.json()["error"]

    r = client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc1, "currency": "RUB",
                                      "quantity": 100, "price": "9,45", "date": "2021-11-15"})
    assert r.status_code == 200, r.text
    r = client.post("/api/transfer", json={"item": "Kilowatt Case", "from_account_id": acc1, "to_account_id": acc2,
                                           "currency": "RUB", "quantity": 30})
    assert r.status_code == 200, r.text
    rows = {row["account"]: row for row in client.get("/api/portfolio").json()["rows"]}
    assert rows["Основной"]["quantity"] == 70 and rows["Твинк"]["quantity"] == 30
    assert rows["Твинк"]["avg_price"] == 9.45 and rows["Твинк"]["first_buy_date"] == "2021-11-15"

    too_many = client.post("/api/sell", json={"item": "Kilowatt Case", "account_id": acc2, "currency": "RUB",
                                              "quantity": 31, "price": "10"})
    assert too_many.status_code == 400 and too_many.json()["violations"]

    assert client.post("/api/position-comment", json={"item": "Kilowatt Case", "account_id": acc2,
                                                      "currency": "RUB", "comment": "Тест"}).status_code == 200
    rows = {row["account"]: row for row in client.get("/api/portfolio").json()["rows"]}
    assert rows["Твинк"]["comment"] == "Тест"

    order = client.post("/api/orders", json={"item": "Kilowatt Case", "account_id": acc1, "currency": "RUB",
                                             "quantity": 5, "price": "9"}).json()["order_id"]
    assert client.post(f"/api/orders/{order}/fill", json={"quantity": 2}).status_code == 200
    [o] = client.get("/api/orders").json()["rows"]
    assert o["quantity"] == 3
    assert client.post(f"/api/orders/{order}/remove").status_code == 200
    assert client.get("/api/orders").json()["rows"] == []

    # аккаунт с операциями удалить нельзя, только скрыть
    assert client.post(f"/api/accounts/{acc2}/delete").status_code == 400
    assert client.post(f"/api/accounts/{acc2}", json={"hidden": True}).status_code == 200
    meta = client.get("/api/meta").json()
    assert [a["hidden"] for a in meta["accounts"] if a["id"] == acc2] == [True]


def test_edit_and_delete_records(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "Основной"}).json()["id"]
    trade = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB"}
    buy = client.post("/api/buy", json={**trade, "quantity": 10, "price": "10", "date": "2026-01-10"}).json()["op_id"]
    sale = client.post("/api/sell", json={**trade, "quantity": 4, "price": "15", "date": "2026-01-20"}).json()["op_id"]
    # покупку с продажей удалить нельзя — ошибка называет продажу
    r = client.post(f"/api/ops/{buy}/delete")
    assert r.status_code == 400 and f"#{sale}" in r.json()["violations"][0]
    # правка меняет саму покупку
    assert client.post(f"/api/ops/{buy}/edit", json={"price": "12", "quantity": 10}).status_code == 200
    [pos] = client.get("/api/portfolio").json()["rows"]
    assert pos["avg_price"] == 12.0 and pos["single_op"] is None  # строка из покупки и продажи
    assert client.post(f"/api/ops/{buy}/edit", json={"price": "12"}).json()["error"] == "ничего не изменилось"
    # удаление продажи — насовсем
    assert client.post(f"/api/ops/{sale}/delete").status_code == 200
    assert client.get("/api/portfolio").json()["rows"][0]["quantity"] == 10
    assert client.get("/journal").status_code == 404 and client.get("/api/journal").status_code == 404


def test_order_fill_comment_in_portfolio_row(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    order = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB", "quantity": 10, "price": "5"}
    first = client.post("/api/orders", json={**order, "comment": "на лето"}).json()["order_id"]
    client.post(f"/api/orders/{first}/fill", json={"quantity": 4})
    second = client.post("/api/orders", json={**order, "comment": "добор"}).json()["order_id"]
    client.post(f"/api/orders/{second}/fill", json={})
    [row] = client.get("/api/portfolio").json()["rows"]
    assert row["quantity"] == 14 and row["comment"] == "на лето; добор"
    assert [o["quantity"] for o in client.get("/api/orders").json()["rows"]] == [6]


def test_fx_endpoint_uses_cbr_before_steam_history(client):
    login(client)
    assert client.get("/api/fx?currency=RUB&on=2021-11-15").json() == {"rate": 80.0, "source": "cbr"}


def test_money_parsing(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    r = client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc, "currency": "RUB", "quantity": 1, "price": "1,234"})
    assert r.status_code == 400


def test_remove_position_endpoint(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc, "currency": "USD", "quantity": 2, "price": "0,50"})
    r = client.post("/api/remove-position", json={"item": "Kilowatt Case", "account_id": acc, "currency": "USD"})
    assert r.status_code == 200
    assert client.get("/api/portfolio").json()["rows"] == []
    assert client.get("/api/stats").json()["summary"]["total_invested_usd"] == 0
    again = client.post("/api/remove-position", json={"item": "Kilowatt Case", "account_id": acc, "currency": "USD"})
    assert again.status_code == 400


def test_stats_series_by_currency(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc, "currency": "RUB", "quantity": 2, "price": "50"})
    assert client.get("/api/stats/series?currency=RUB").json()["currency"] == "RUB"
    assert client.get("/api/stats/series?currency=TOTAL").json()["currency"] == "USD"
    assert client.get("/api/stats/series?currency=KZT").json()["value"] == []  # любая валюта Steam
    assert client.get("/api/stats/series?currency=BYN").status_code == 400  # Steam её не поддерживает
    assert [c["code"] for c in client.get("/api/stats").json()["chart_currencies"]] == ["RUB"]
    assert len(client.get("/api/meta").json()["currencies"]) == 39


def test_portfolio_tabs_and_row_purchases(client):
    login(client)
    assert client.get("/orders", follow_redirects=False).headers["location"] == "/portfolio#orders"
    page = client.get("/portfolio").text
    assert "Текущий портфель" in page and "Закрытые позиции" in page and 'id="o-table"' in page
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    acc2 = client.post("/api/accounts", json={"name": "B"}).json()["id"]
    trade = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB"}
    one = client.post("/api/buy", json={**trade, "quantity": 5, "price": "10"}).json()["op_id"]
    [row] = client.get("/api/portfolio").json()["rows"]
    assert row["single_op"]["op_id"] == one and [b["op_id"] for b in row["buys"]] == [one]
    # правка из ячейки — это правка самой покупки
    assert client.post(f"/api/ops/{one}/edit", json={"quantity": 7}).status_code == 200
    two = client.post("/api/buy", json={**trade, "quantity": 1, "price": "20"}).json()["op_id"]
    [row] = client.get("/api/portfolio").json()["rows"]
    assert row["quantity"] == 8 and row["single_op"] is None and [b["op_id"] for b in row["buys"]] == [one, two]
    client.post("/api/transfer", json={**trade, "from_account_id": acc, "to_account_id": acc2, "quantity": 2})
    moved = next(r for r in client.get("/api/portfolio").json()["rows"] if r["account_id"] == acc2)
    # получена переносом — это обычная покупка по средней цене исходной строки, её можно править в ячейке
    assert moved["single_op"]["kind"] == "buy" and moved["quantity"] == 2 and moved["first_buy_date"] == row["first_buy_date"]


def test_row_reopened_same_day_edits_in_cell(client):
    # Всё перенесли и в тот же день купили снова — новая строка из одной покупки правится в ячейке.
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    acc2 = client.post("/api/accounts", json={"name": "B"}).json()["id"]
    trade = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB"}
    client.post("/api/buy", json={**trade, "quantity": 10, "price": "15", "date": "2024-11-02"})
    client.post("/api/transfer", json={**trade, "from_account_id": acc, "to_account_id": acc2, "quantity": 10,
                                       "comment": "с A"})
    again = client.post("/api/buy", json={**trade, "quantity": 7, "price": "15"}).json()["op_id"]
    rows = {r["account_id"]: r for r in client.get("/api/portfolio").json()["rows"]}
    assert rows[acc]["single_op"]["op_id"] == again
    assert rows[acc2]["first_buy_date"] == "2024-11-02" and rows[acc2]["comment"] == "с A"


def test_removed_purchases_are_not_part_of_row(client):
    # Две ошибочные покупки удалены, потом внесена покупка задним числом — строка состоит только из неё.
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    trade = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB"}
    for quantity in (200, 1200):
        bought = client.post("/api/buy", json={**trade, "quantity": quantity, "price": "100", "date": "2026-10-31"})
        assert bought.status_code == 200, bought.text
        removed = client.post("/api/remove-position", json=trade)
        assert removed.status_code == 200, removed.text
    real = client.post("/api/buy", json={**trade, "quantity": 500, "price": "100", "date": "2026-10-27"}).json()["op_id"]
    [row] = client.get("/api/portfolio").json()["rows"]
    assert row["quantity"] == 500 and [b["op_id"] for b in row["buys"]] == [real] and row["single_op"]["op_id"] == real


def test_delete_closed_row_returns_to_portfolio(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    trade = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB", "quantity": 3}
    client.post("/api/buy", json={**trade, "price": "10"})
    client.post("/api/sell", json={**trade, "price": "50"})
    [sale] = client.get("/api/portfolio?closed=true").json()["rows"]
    assert client.get("/api/portfolio").json()["rows"] == []
    assert client.post(f"/api/ops/{sale['op_id']}/delete").status_code == 200
    assert client.get("/api/portfolio?closed=true").json()["rows"] == []
    assert client.get("/api/portfolio").json()["rows"][0]["quantity"] == 3
    assert client.get("/api/stats").json()["summary"]["total_profit_usd"] == 0


def test_settings_page_and_api(client):
    assert client.get("/api/settings").status_code == 401
    login(client)
    assert "Уведомления о ценах" in client.get("/settings").text
    s = client.get("/api/settings").json()
    assert s["alerts"]["min_price_usd"] == 0.1 and s["telegram"] == {"token": False, "chat": None}
    body = {**s["alerts"], "min_price_usd": 0.25, "day_pct": 40}
    assert client.post("/api/settings/alerts", json=body).json()["min_price_usd"] == 0.25
    assert client.get("/api/settings").json()["alerts"]["day_pct"] == 40
    bad = client.post("/api/settings/alerts", json={**body, "week3_pct": 50})
    assert bad.status_code == 400 and "×3" in bad.json()["error"]
    assert client.post("/api/settings/telegram-test").status_code == 400  # бот не подключён
