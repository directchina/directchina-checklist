import hashlib
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from checklist import cloud_cookies
from checklist import timeweb_cloud as cloud
from checklist.core import CheckError, Report


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_cookies, "STATE", tmp_path / "state")
    path = tmp_path / "Default"
    path.mkdir()
    with sqlite3.connect(path / "Cookies") as db:
        db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('version', '24')")
        db.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, path TEXT, value TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
    monkeypatch.setenv("TIMEWEB_CLOUD_AUTH", "chromium")
    monkeypatch.setenv("TIMEWEB_CLOUD_CHROMIUM_PROFILE", str(path))
    return path


def insert_cookie(
    profile,
    name="refresh_token",
    value="browser-secret",
    host=".timeweb.cloud",
    expires=0,
    encrypted=None,
):
    if encrypted is None:
        plain = hashlib.sha256(host.encode()).digest() + value.encode()
        pad = padding.PKCS7(128).padder()
        padded = pad.update(plain) + pad.finalize()
        key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
        enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
        encrypted = b"v10" + enc.update(padded) + enc.finalize()
    with sqlite3.connect(profile / "Cookies") as db:
        db.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?)",
            (host, name, "/", "", encrypted, expires),
        )


def test_cookie_scope_and_database_unchanged(profile):
    insert_cookie(profile)
    insert_cookie(profile, name="unrelated", encrypted=b"v11-invalid")
    insert_cookie(profile, host=".other.test", encrypted=b"v11-invalid")
    insert_cookie(profile, host="evil.timeweb.cloud", encrypted=b"v11-invalid")
    insert_cookie(profile, name="device_token", expires=1)
    before = (profile / "Cookies").read_bytes()
    assert cloud_cookies.chromium_cookies(profile) == [
        (".timeweb.cloud", "refresh_token", "/", "browser-secret")
    ]
    assert (profile / "Cookies").read_bytes() == before


@pytest.mark.parametrize("encrypted", [b"v11-private-secret", b"v10-malformed"])
def test_cookie_errors_hide_secrets(profile, encrypted):
    insert_cookie(profile, encrypted=encrypted)
    with pytest.raises(CheckError) as exc:
        cloud_cookies.chromium_cookies(profile)
    assert "private-secret" not in str(exc.value)


def test_missing_or_expired_session(profile):
    insert_cookie(profile, expires=1)
    with pytest.raises(CheckError, match="Нет действующей"):
        cloud_cookies.chromium_cookies(profile)


def test_refresh_rotation_persists_and_new_browser_login_replaces_cache(profile):
    insert_cookie(profile)
    seen = []

    def serve(request):
        seen.append(request.headers.get("cookie", ""))
        assert request.url.path == cloud.REFRESH
        return httpx.Response(
            201,
            json={
                "tokens": {
                    "access_token": "access-secret",
                    "refresh_token": f"rotated-{len(seen)}",
                }
            },
        )

    for _ in range(2):
        with cloud.Cloud(httpx.MockTransport(serve)) as client:
            client.login()
            assert client.client.headers["Authorization"] == "Bearer access-secret"
    assert "browser-secret" in seen[0]
    assert "rotated-1" in seen[1] and "browser-secret" not in seen[1]
    cache = next(cloud_cookies.STATE.glob("*.json"))
    assert cache.stat().st_mode & 0o777 == 0o600
    assert "rotated-2" in cache.read_text()
    assert "access-secret" not in cache.read_text()
    with sqlite3.connect(profile / "Cookies") as db:
        db.execute("DELETE FROM cookies")
    insert_cookie(profile, value="new-browser-login")
    with cloud.Cloud(httpx.MockTransport(serve)) as client:
        client.login()
    assert "new-browser-login" in seen[-1]


def test_session_lock_prevents_simultaneous_refresh(profile):
    insert_cookie(profile)
    with (
        cloud_cookies.cloud_session(profile),
        pytest.raises(CheckError, match="Другой обход"),
        cloud_cookies.cloud_session(profile),
    ):
        pytest.fail("Concurrent refresh allowed")


def test_password_login(monkeypatch):
    monkeypatch.setenv("TIMEWEB_CLOUD_AUTH", "password")
    monkeypatch.setenv("TIMEWEB_CLOUD_LOGIN", "demo")
    monkeypatch.setenv("TIMEWEB_CLOUD_PASSWORD", " password ")

    def serve(request):
        assert request.method == "POST" and request.url.path == cloud.LOGIN
        assert (
            request.headers["Authorization"]
            == httpx.BasicAuth("demo", " password ")._auth_header
        )
        assert json.loads(request.content) == {}
        return httpx.Response(201, json={"tokens": {"access_token": "access-secret"}})

    with cloud.Cloud(httpx.MockTransport(serve)) as client:
        client.login()


def test_cloud_blocks_mutations_foreign_urls_and_redirects():
    calls = []
    with cloud.Cloud(
        httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={}))
    ) as client:
        for method, path in [
            ("PUT", "/api/v2/news/counters"),
            ("POST", cloud.FINANCES),
            ("DELETE", "/api/v1/servers/123"),
            ("GET", "/api/v2/news/1"),
            ("POST", "https://elsewhere.test/api/v4/auth"),
            ("GET", cloud.FINANCES + "?redirect=evil"),
        ]:
            with pytest.raises(CheckError):
                client.request(method, path)
        with pytest.raises(CheckError):
            client.request("GET", cloud.FINANCES, follow_redirects=True)
    assert not calls


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
def test_http_errors_never_expose_body(status):
    with (
        cloud.Cloud(
            httpx.MockTransport(
                lambda r: httpx.Response(
                    status,
                    text="private-secret",
                    headers={"Location": "https://elsewhere.test"},
                )
            )
        ) as client,
        pytest.raises(CheckError) as exc,
    ):
        client.request("POST", cloud.REFRESH)
    assert "private-secret" not in str(exc.value)


def responses():
    return {
        cloud.FINANCES: {
            "finances": {
                "balance": 1000,
                "currency": "RUB",
                "monthly_cost": 3000,
                "hourly_cost": 4,
                "hours_left": 240,
            }
        },
        cloud.NEWS: {"unread_news_count": 2},
        cloud.LEGACY_NOTIFICATIONS: {
            "meta": {"count": 1},
            "notifications": [{"id": 1, "message": "old"}],
        },
        cloud.NOTIFICATIONS: {
            "meta": {"total": 1},
            "notifications": [{"id": 1, "message": "<b>maintenance</b>"}],
        },
        cloud.STATUS: {"status": {"is_blocked": False, "is_permanent_blocked": False}},
        cloud.cloud_servers.SERVERS: {"servers": [], "meta": {"total": 0}},
    }


def run_fake(monkeypatch, data):
    requests = []

    def serve(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=data[request.url.path])

    factory = cloud.Cloud
    monkeypatch.setattr(cloud, "Cloud", lambda: factory(httpx.MockTransport(serve)))
    monkeypatch.setattr(factory, "login", lambda self: None)
    report = Report()
    cloud.run(report, {}, datetime(2026, 9, 21, tzinfo=ZoneInfo("Europe/Moscow")))
    assert requests == [
        ("GET", p)
        for p in (
            cloud.FINANCES,
            cloud.NEWS,
            cloud.LEGACY_NOTIFICATIONS,
            cloud.NOTIFICATIONS,
            cloud.STATUS,
            cloud.cloud_servers.SERVERS,
        )
    ]
    return report


def test_complete_report_and_deduplicated_banners(monkeypatch):
    report = run_fake(monkeypatch, responses())
    assert report.exit_code == 1
    findings = {f.check: f for f in report.findings}
    assert findings["Запас средств"].status == "WARN"
    assert "10.0 дн." in findings["Запас средств"].detail
    assert findings["Уведомления"].detail == "Баннеров кабинета: 1; maintenance"
    assert findings["Аккаунт"].status == "OK"


@pytest.mark.parametrize(
    "field, value", [("balance", None), ("currency", "USD"), ("hours_left", None)]
)
def test_invalid_finances_isolated(monkeypatch, field, value):
    data = responses()
    data[cloud.FINANCES]["finances"][field] = value
    report = run_fake(monkeypatch, data)
    assert report.exit_code == 2
    assert next(f for f in report.findings if f.check == "Аккаунт").status == "OK"


def test_incomplete_notifications_are_error(monkeypatch):
    data = responses()
    data[cloud.NOTIFICATIONS]["meta"]["total"] = 10
    report = run_fake(monkeypatch, data)
    assert any(
        f.check == "Уведомления" and f.status == "ERROR" for f in report.findings
    )
