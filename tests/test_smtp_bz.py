from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from checklist import smtp_bz
from checklist.__main__ import SERVICES
from checklist.core import CheckError, Report, clean, required

BALANCE_PAGE = '<nav><a href="/panel/billing">Мои финансы: 1 200 р</a></nav>'


def serve(rows, *, balance_page=BALANCE_PAGE, login_status=302):
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "GET" and request.url.path == "/login":
            return httpx.Response(200, text='<form action="/login"><input name="email"></form>')
        if request.method == "POST" and request.url.path == "/login":
            return httpx.Response(login_status, headers={"location": "/panel/"} if login_status == 302 else {}, text="Sign in")
        if request.method == "GET" and request.url.path == "/panel/":
            return httpx.Response(200, text=balance_page)
        if request.method == "GET" and request.url.path == "/panel/pagedata/sentlogs.php":
            return httpx.Response(200, json={"data": rows, "recordsTotal": len(rows), "recordsFiltered": len(rows)})
        return httpx.Response(404)

    return httpx.MockTransport(respond), calls


def row(status):
    return ["1", "sender", "recipient", "subject", "", "", status + '<br><a href="/message">Подробнее</a>', "2026-10-06 12:10:34"]


def test_parses_live_shaped_balance_and_recent_delivery_failures(monkeypatch):
    monkeypatch.setenv("SMTPBZ_LOGIN", "demo@example.test")
    monkeypatch.setenv("SMTPBZ_PASS", " private password ")
    transport, calls = serve([row("Доставлено") for _ in range(97)] + [row("Не существует") for _ in range(3)])
    report = Report()
    smtp_bz.run(report, {}, datetime.now(ZoneInfo("Europe/Moscow")), transport=transport)
    assert [(f.check, f.status, f.detail) for f in report.findings] == [
        ("Баланс", "OK", "1 200.00 ₽"),
        ("Последние отправки", "OK", "Ошибки доставки: 3 из 100 (3%); доставлено: 97. Предупреждение от 5%."),
    ]
    assert [(c.method, c.url.path) for c in calls] == [
        ("GET", "/login"), ("POST", "/login"),
        ("GET", "/panel/"), ("GET", "/panel/pagedata/sentlogs.php"),
    ]
    assert all(c.url.host == "smtp.bz" for c in calls)
    assert calls[-1].url.params["length"] == "100"
    assert calls[1].content == b"email=demo%40example.test&password=+private+password+"


def test_many_errors_raise_warning():
    rows = [row("Возвращено") for _ in range(5)] + [row("Доставлено") for _ in range(95)]
    transport, _ = serve(rows)
    findings = smtp_bz.check("email", "password", transport=transport)
    assert findings[1].status == "WARN"
    assert "5 из 100 (5%)" in findings[1].detail


def test_undelivered_and_retry_are_not_reported_as_delivered():
    transport, _ = serve([row("Отменено"), row("Повтор"), row("Отправлено"), row("Доставлено")])
    findings = smtp_bz.check("email", "password", transport=transport)
    assert findings[1].status == "WARN"
    assert "Ошибки доставки: 1 из 4 (25%)" in findings[1].detail
    assert "доставлено: 1" in findings[1].detail
    assert "в обработке/повторе: 2" in findings[1].detail


def test_unknown_status_fails_closed_without_addresses():
    transport, _ = serve([row("SECRET address@private.test")])
    with pytest.raises(CheckError, match="Неизвестный статус отправки") as error:
        smtp_bz.check("email", "password", transport=transport)
    assert "SECRET" not in str(error.value)


def test_login_failure_and_html_instead_of_json_are_errors():
    transport, _ = serve([], login_status=200)
    with pytest.raises(CheckError, match="вход"):
        smtp_bz.check("email", "password", transport=transport)
    transport, _ = serve([], balance_page="Страница входа")
    with pytest.raises(CheckError, match="баланс"):
        smtp_bz.check("email", "password", transport=transport)


def test_secret_pass_is_preserved_and_redacted(monkeypatch):
    monkeypatch.setenv("SMTPBZ_PASS", " spaced-secret ")
    assert required("SMTPBZ_PASS") == " spaced-secret "
    assert "spaced-secret" not in clean("bad spaced-secret response")


def test_negative_balance_is_reported_as_warning():
    transport, _ = serve([row("Доставлено")], balance_page='<a href="/panel/billing">Мои финансы: -12,50 р</a>')
    findings = smtp_bz.check("email", "password", transport=transport)
    assert findings[0].status == "WARN"
    assert findings[0].detail == "-12.50 ₽"


def test_rejects_short_result_page_and_never_claims_no_errors():
    transport, _ = serve([row("Доставлено") for _ in range(5)])
    findings = smtp_bz.check("email", "password", transport=transport)
    assert "из 5" in findings[1].detail
    assert "из 100" not in findings[1].detail
    assert findings[1].status == "OK"


def test_registered_in_full_check_and_available_as_separate_service():
    assert SERVICES["smtp_bz"] == ("SMTP.BZ", smtp_bz.run)
