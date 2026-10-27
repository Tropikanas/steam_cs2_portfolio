from steamfolio import health

URL = "https://example.test/login"


def test_alert_after_two_failures_then_recovery(conn):
    sent: list[str] = []
    send = lambda text: sent.append(text) or True  # noqa: E731
    assert health.update(conn, URL, "ConnectError", send)["fails"] == 1
    assert sent == []  # одна неудача — молчим
    health.update(conn, URL, "ConnectError", send)
    assert len(sent) == 1 and "не открывается (2 проверки подряд)" in sent[0]
    health.update(conn, URL, "ConnectError", send)
    assert len(sent) == 1  # уже предупредили
    assert health.update(conn, URL, None, send) == {"fails": 0, "alerted": False, "last_error": "ConnectError"}
    assert "снова открывается" in sent[1]
    health.update(conn, URL, None, send)
    assert len(sent) == 2


def test_single_failure_recovers_silently(conn):
    sent: list[str] = []
    health.update(conn, URL, "timeout", lambda t: sent.append(t) or True)
    health.update(conn, URL, None, lambda t: sent.append(t) or True)
    assert sent == []


def test_alert_retried_when_telegram_not_configured(conn):
    health.update(conn, URL, "err", lambda t: False)
    assert health.update(conn, URL, "err", lambda t: False)["alerted"] is False
    sent: list[str] = []
    health.update(conn, URL, "err", lambda t: sent.append(t) or True)
    assert len(sent) == 1


def test_probe_reports_connection_error():
    assert health.probe("http://127.0.0.1:9/login", timeout=2) is not None
