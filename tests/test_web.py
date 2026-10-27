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
    settings = replace(load_settings(), db_path=tmp_path / "web.db")
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


def test_journal_rollback_edit_delete(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "Основной"}).json()["id"]
    trade = {"item": "Kilowatt Case", "account_id": acc, "currency": "RUB"}
    buy = client.post("/api/buy", json={**trade, "quantity": 10, "price": "10", "date": "2026-01-10"}).json()["op_id"]
    sale = client.post("/api/sell", json={**trade, "quantity": 4, "price": "15", "date": "2026-01-20"}).json()["op_id"]

    rows = {r["id"]: r for r in client.get("/api/journal").json()["rows"]}
    assert rows[buy]["kind_name"] == "покупка"
    assert rows[buy]["sum_usd"] == 1.25  # 100 ₽ по курсу 80
    assert rows[buy]["fx_source"] == "cbr"
    assert rows[buy]["can_edit"] is True

    # откат покупки запрещён, пока есть продажа; предпросмотр называет её
    preview = client.get(f"/api/journal/{buy}/preview?action=cancel").json()
    assert preview["allowed"] is False and f"#{sale}" in preview["violations"][0]
    assert client.post(f"/api/journal/{buy}/cancel").status_code == 400

    # откат продажи: прибыль уходит из статистики, позиция возвращается
    preview = client.get(f"/api/journal/{sale}/preview?action=cancel").json()
    assert preview["allowed"] is True
    assert preview["positions"][0]["quantity_before"] == 6 and preview["positions"][0]["quantity_after"] == 10
    assert preview["sales"] == [{"op_id": sale, "item": "Kilowatt Case", "currency": "RUB", "profit_before": 20.0,
                                 "profit_after": None, "profit_usd_before": 0.25, "profit_usd_after": None}]
    assert client.post(f"/api/journal/{sale}/cancel").status_code == 200
    rows = {r["id"]: r for r in client.get("/api/journal").json()["rows"]}
    assert rows[sale]["cancelled"] is True and rows[sale]["history"][0]["action"] == "cancel"
    assert client.post(f"/api/journal/{sale}/restore").status_code == 200

    # изменение: отдельная строка «было → стало», исходная помечена
    edit = client.post(f"/api/journal/{buy}/edit", json={"price": "12", "quantity": 10}).json()["op_id"]
    rows = {r["id"]: r for r in client.get("/api/journal").json()["rows"]}
    assert rows[edit]["kind_name"] == "покупка изменена"
    assert rows[edit]["changes"] == [{"field": "цена", "before": "10,00 RUB", "after": "12,00 RUB"}]
    assert rows[buy]["edited_by"] == [edit]
    assert rows[buy]["current"]["price"] == 12.0
    [pos] = client.get("/api/portfolio").json()["rows"]
    assert pos["avg_price"] == 12.0
    assert client.post(f"/api/journal/{buy}/edit", json={"price": "12"}).json()["error"] == "ничего не изменилось"

    # удаление продажи и откат удаления
    deletion = client.post(f"/api/journal/{sale}/delete").json()["op_id"]
    assert client.get("/api/portfolio").json()["rows"][0]["quantity"] == 10
    assert client.post(f"/api/journal/{deletion}/cancel").status_code == 200
    assert client.get("/api/portfolio").json()["rows"][0]["quantity"] == 6


def test_fx_endpoint_uses_cbr_before_steam_history(client):
    login(client)
    assert client.get("/api/fx?currency=RUB&on=2021-11-15").json() == {"rate": 80.0, "source": "cbr"}


def test_money_parsing(client):
    login(client)
    acc = client.post("/api/accounts", json={"name": "A"}).json()["id"]
    r = client.post("/api/buy", json={"item": "Kilowatt Case", "account_id": acc, "currency": "RUB", "quantity": 1, "price": "1,234"})
    assert r.status_code == 400
