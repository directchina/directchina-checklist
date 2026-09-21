import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from checklist import crm
from checklist.core import Api, CheckError, Report, required
from checklist.session import Cabinet


def test_smartvhod_mutations_never_reach_network():
    calls = []
    transport = httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={})
    )
    with Api("https://smartvhod.ru", transport=transport) as client:
        for path, login in [
            ("/api/keys/update-settings", True),
            ("/api/multi/renew", True),
            ("/api/keys/delete-device", False),
            ("https://elsewhere.test/", False),
        ]:
            with pytest.raises(CheckError):
                client.request(path, login=login)
        client.request("/api/user/profile")
        client.request(
            "/api/auth/login",
            login=True,
            json={"telegram_id": "demo", "password": "test"},
        )
    assert [(c.method, c.url.path) for c in calls] == [
        ("GET", "/api/user/profile"),
        ("POST", "/api/auth/login"),
    ]


def test_cabinet_rejects_writes_and_foreign_credentials():
    calls = []
    transport = httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)
    )
    with Cabinet("https://e-x1.aspro.cloud", [crm.NEWS], transport) as client:
        for method, path in [
            ("POST", "/mark-read"),
            ("PUT", crm.NEWS),
            ("DELETE", crm.NEWS),
            ("POST", "https://evil.test/login"),
            ("GET", "/_module/system/rest/notifications/counter_unread"),
        ]:
            with pytest.raises(CheckError):
                client._request(method, path)
    assert not calls


def test_auth_redirect_to_another_origin_is_blocked(monkeypatch):
    monkeypatch.setenv("ASPRO_LOGIN", "demo")
    monkeypatch.setenv("ASPRO_PASSWORD", "secret")
    calls = []

    def serve(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, text='<input name="_csrf_token" value="test">')
        return httpx.Response(
            302, headers={"Location": "https://evil.test/?_auth_token=private"}
        )

    with (
        Cabinet("https://e-x1.aspro.cloud", [], httpx.MockTransport(serve)) as client,
        pytest.raises(CheckError),
    ):
        client.login_crm()
    assert all(c.url.host == "e-x1.aspro.cloud" for c in calls)


def test_crm_reads_system_news_only(monkeypatch):
    calls = []

    class FakeCabinet:
        def __init__(self, origin, reads):
            assert reads == [crm.NEWS]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def login_crm(self):
            pass

        def json(self, path):
            calls.append(path)
            return {"result": True, "has_unread": False}

    monkeypatch.setattr(crm, "Cabinet", FakeCabinet)
    report = Report()
    crm.run(report, {}, datetime.now(ZoneInfo("UTC")))
    assert calls == [crm.NEWS]
    assert report.exit_code == 0
    assert len(report.findings) == 1


def test_auth_errors_do_not_expose_body():
    with Api(
        "https://smartvhod.ru",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(401, text="private-password")
        ),
    ) as client:
        with pytest.raises(CheckError) as exc:
            client.request("/api/user/profile")
        assert "private-password" not in str(exc.value)


def test_password_whitespace_is_preserved(monkeypatch):
    monkeypatch.setenv("SMARTVHOD_PASSWORD", " a b ")
    assert required("SMARTVHOD_PASSWORD") == " a b "


def test_failure_is_visible_but_other_checks_continue(monkeypatch):
    monkeypatch.setenv("SMARTVHOD_PASSWORD", "secret-value")
    report = Report()
    report.attempt(
        "A", "balance", lambda: (_ for _ in ()).throw(RuntimeError("secret-value"))
    )
    report.add("B", "news", "secret-value\x1b[31mtest")
    assert report.exit_code == 2
    assert "secret-value" not in json.dumps([f.detail for f in report.findings])
    assert report.findings[1].detail == "***test"
