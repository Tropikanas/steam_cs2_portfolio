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


def rows_of(client, closed=False):
    return client.get(f"/api/portfolio?closed={'true' if closed else 'false'}").json()["rows"]


def test_buy_transfer_and_order_flow(client):
    assert login(client).status_code == 303
    acc1 = client.post("/api/accounts", json={"name": "Основной"}).json()["id"]
    acc2 = client.post("/api/accounts", json={"name": "Твинк"}).json()["id"]

    bad = client.post("/api/buy", json={"item": "Нет такого", "account_id": acc1, "currency": "RUB", "quantity": 1, "price": "1"})
    assert bad.status_code == 400 and "справочнике" in bad.json()["error"]

    r = client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc1, "currency": "RUB",
                                      "quantity": 100, "price": "9,45", "date": "2021-11-15"})
    assert r.status_code == 200, r.text
    lot = r.json()["op_id"]
    r = client.post("/api/transfer", json={"lot_id": lot, "to_account_id": acc2, "quantity": 30})
    assert r.status_code == 200, r.text
    rows = {row["account"]: row for row in rows_of(client)}
    assert rows["Основной"]["quantity"] == 70 and rows["Твинк"]["quantity"] == 30
    assert rows["Твинк"]["avg_price"] == 9.45 and rows["Твинк"]["first_buy_date"] == "2021-11-15"

    too_many = client.post("/api/sell", json={"lot_id": rows["Твинк"]["lot_id"], "quantity": 31, "price": "10"})
    assert too_many.status_code == 400 and too_many.json()["violations"]

    # комментарий строки — комментарий её покупки
    assert client.post(f"/api/ops/{rows['Твинк']['lot_id']}/edit", json={"comment": "Тест"}).status_code == 200
    rows = {row["account"]: row for row in rows_of(client)}
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
    sale = client.post("/api/sell", json={"lot_id": buy, "quantity": 4, "price": "15", "date": "2026-01-20"}).json()["op_id"]
    # покупку с продажей удалить нельзя — ошибка называет продажу
    r = client.post(f"/api/ops/{buy}/delete")
    assert r.status_code == 400 and f"#{sale}" in r.json()["violations"][0]
    # правка меняет саму покупку; строка — по-прежнему одна покупка
    assert client.post(f"/api/ops/{buy}/edit", json={"price": "12", "quantity": 10}).status_code == 200
    [pos] = rows_of(client)
    assert (pos["avg_price"], pos["quantity"], pos["bought"], pos["single_op"]["op_id"]) == (12.0, 6, 10, buy)
    assert client.post(f"/api/ops/{buy}/edit", json={"price": "12"}).json()["error"] == "ничего не изменилось"
    # удаление продажи — насовсем, штуки возвращаются в свою строку
    assert client.post(f"/api/ops/{sale}/delete").status_code == 200
    assert rows_of(client)[0]["quantity"] == 10
    assert client.get("/journal").status_code == 404 and client.get("/api/journal").status_code == 404


def test_edit_sale_in_closed_positions(client):
    # Продажу правят в «Закрытых позициях»: количество возвращается в портфель или уходит из него.
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    lot = client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc, "currency": "RUB",
                                        "quantity": 10, "price": "10", "date": "2026-01-10"}).json()["op_id"]
    sale = client.post("/api/sell", json={"lot_id": lot, "quantity": 4, "price": "15", "date": "2026-01-20"}).json()["op_id"]
    assert client.post(f"/api/ops/{sale}/edit", json={"quantity": 6, "price": "20", "date": "2026-01-25"}).status_code == 200
    [closed] = rows_of(client, closed=True)
    assert (closed["quantity"], closed["sale_price"], closed["sale_date"], closed["profit"]) == (6, 20.0, "2026-01-25", 60.0)
    assert closed["position_no"] == rows_of(client)[0]["position_no"] == 1  # продано из позиции ID 1
    assert client.get("/api/portfolio?collapse=true").json()["rows"][0]["position_no"] is None
    assert rows_of(client)[0]["quantity"] == 4
    assert client.post(f"/api/ops/{sale}/edit", json={"quantity": 11}).status_code == 400  # больше, чем куплено
    assert client.post(f"/api/ops/{sale}/edit", json={"quantity": 2}).status_code == 200
    assert rows_of(client)[0]["quantity"] == 8
    assert client.get("/api/stats").json()["summary"]["total_sold_usd"] == 0.5  # 2 × 20 ₽ по курсу 80


def test_each_order_fill_is_its_own_row(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    order = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB", "quantity": 10, "price": "5"}
    first = client.post("/api/orders", json={**order, "comment": "на лето"}).json()["order_id"]
    client.post(f"/api/orders/{first}/fill", json={"quantity": 4})
    client.post(f"/api/orders/{first}/fill", json={"quantity": 2})
    second = client.post("/api/orders", json={**order, "comment": "добор"}).json()["order_id"]
    client.post(f"/api/orders/{second}/fill", json={})
    assert sorted((r["quantity"], r["comment"]) for r in rows_of(client)) == [(2, "на лето"), (4, "на лето"), (10, "добор")]
    [merged] = client.get("/api/portfolio?collapse=true").json()["rows"]
    assert (merged["quantity"], merged["lots"], merged["comment"], merged["single_op"]) == (16, 3, "на лето; добор", None)
    assert [o["quantity"] for o in client.get("/api/orders").json()["rows"]] == [4]


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
    lot = client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc, "currency": "USD", "quantity": 2,
                                        "price": "0,50"}).json()["op_id"]
    r = client.post("/api/remove-position", json={"lot_id": lot})
    assert r.status_code == 200 and r.json()["removed"] == 2
    assert rows_of(client) == []
    assert client.get("/api/stats").json()["summary"]["total_invested_usd"] == 0
    assert client.post("/api/remove-position", json={"lot_id": lot}).status_code == 400


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


def test_portfolio_tabs_and_rows_per_purchase(client):
    login(client)
    assert client.get("/orders", follow_redirects=False).headers["location"] == "/portfolio#orders"
    page = client.get("/portfolio").text
    assert "Текущий портфель" in page and "Закрытые позиции" in page and 'id="o-table"' in page
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    acc2 = client.post("/api/accounts", json={"name": "B"}).json()["id"]
    trade = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB"}
    one = client.post("/api/buy", json={**trade, "quantity": 5, "price": "10"}).json()["op_id"]
    [row] = rows_of(client)
    assert row["single_op"]["op_id"] == one and row["lot_id"] == one
    assert client.post(f"/api/ops/{one}/edit", json={"quantity": 7}).status_code == 200
    two = client.post("/api/buy", json={**trade, "quantity": 1, "price": "20"}).json()["op_id"]
    # покупки не складываются: две строки, у каждой своя цена
    assert sorted((r["lot_id"], r["quantity"], r["avg_price"]) for r in rows_of(client)) == [(one, 7, 10.0), (two, 1, 20.0)]
    client.post("/api/transfer", json={"lot_id": one, "to_account_id": acc2, "quantity": 2})
    moved = next(r for r in rows_of(client) if r["account_id"] == acc2)
    # получена переносом — это обычная покупка по цене исходной строки, её можно править в ячейке
    assert moved["single_op"]["kind"] == "buy" and moved["quantity"] == 2 and moved["avg_price"] == 10.0


def test_transfer_carries_date_and_comment(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    acc2 = client.post("/api/accounts", json={"name": "B"}).json()["id"]
    lot = client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc, "currency": "RUB", "quantity": 10,
                                        "price": "15", "date": "2024-11-02"}).json()["op_id"]
    client.post("/api/transfer", json={"lot_id": lot, "to_account_id": acc2, "quantity": 10, "comment": "с A"})
    [row] = rows_of(client)
    assert (row["account_id"], row["first_buy_date"], row["comment"]) == (acc2, "2024-11-02", "с A")


def test_delete_closed_row_returns_to_its_row(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    trade = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB"}
    first = client.post("/api/buy", json={**trade, "quantity": 3, "price": "10"}).json()["op_id"]
    client.post("/api/buy", json={**trade, "quantity": 5, "price": "30"})
    client.post("/api/sell", json={"lot_id": first, "quantity": 3, "price": "50"})
    [sale] = rows_of(client, closed=True)
    assert (sale["lot_id"], sale["avg_price"], sale["profit"]) == (first, 10.0, 120.0)
    assert [r["quantity"] for r in rows_of(client)] == [5]
    assert client.post(f"/api/ops/{sale['op_id']}/delete").status_code == 200
    assert rows_of(client, closed=True) == []
    assert sorted((r["lot_id"] == first, r["quantity"]) for r in rows_of(client)) == [(False, 5), (True, 3)]
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
