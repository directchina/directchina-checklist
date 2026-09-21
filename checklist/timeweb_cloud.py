"""Cloud cabinet endpoints: authentication and fixed read-only requests."""

import os
from datetime import timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from .cloud_cookies import HOSTS, NAMES, cloud_session
from .core import CheckError, number, records, required, rub

SERVICE = "Timeweb Cloud"
ORIGIN = "https://timeweb.cloud"
LOGIN = "/api/v4/auth"
REFRESH = "/api/v1/auth/update-token"
FINANCES = "/api/v1/account/finances"
NEWS = "/api/v2/unread-count/news"
NOTIFICATIONS = "/api/v2/notifications"
LEGACY_NOTIFICATIONS = "/api/v1/notifications"
STATUS = "/api/v1/account/status"
READS = frozenset((FINANCES, NEWS, NOTIFICATIONS, LEGACY_NOTIFICATIONS, STATUS))


class Cloud:
    def __init__(self, transport=None):
        self.client = httpx.Client(
            base_url=ORIGIN, timeout=25, follow_redirects=False, transport=transport
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def request(self, method, path, **kwargs):
        if (
            str(self.client.base_url).rstrip("/") != ORIGIN
            or not (
                method == "GET"
                and path in READS
                or method == "POST"
                and path in (LOGIN, REFRESH)
            )
            or kwargs.get("follow_redirects")
        ):
            raise CheckError(
                "Запрос отсутствует в списке разрешённых чтений/авторизации Cloud"
            )
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise CheckError("Таймаут Timeweb Cloud (25 с)") from None
        except httpx.RequestError:
            raise CheckError("Ошибка сети, DNS или TLS Timeweb Cloud") from None
        if (
            response.status_code in (401, 403)
            or path == REFRESH
            and response.status_code == 404
        ):
            raise CheckError(
                "Cloud отклонил вход или доступ: обновите сессию Chromium "
                "либо проверьте логин, пароль и права"
            )
        if not 200 <= response.status_code < 300:
            raise CheckError(f"HTTP {response.status_code}: запрос Cloud не выполнен")
        try:
            data = response.json()
        except ValueError:
            raise CheckError(
                "Cloud вернул не JSON; возможна страница входа или защиты"
            ) from None
        if not isinstance(data, dict) or data.get("error_code") or data.get("error"):
            raise CheckError("Cloud вернул ошибку или неожиданный формат ответа")
        return data

    def login(self):
        mode = os.environ.get("TIMEWEB_CLOUD_AUTH", "auto").strip()
        profile = Path(
            os.environ.get(
                "TIMEWEB_CLOUD_CHROMIUM_PROFILE",
                str(Path.home() / ".config/chromium/Default"),
            )
        ).expanduser()
        if mode not in {"auto", "chromium", "password"}:
            raise CheckError("TIMEWEB_CLOUD_AUTH: допустимы auto, chromium, password")
        use_chromium = (
            mode == "chromium"
            or mode == "auto"
            and any(
                (profile / name).is_file() for name in ("Cookies", "Network/Cookies")
            )
        )
        if use_chromium:
            with cloud_session(profile) as (cookies, save):
                for host, name, path, value in cookies:
                    self.client.cookies.set(name, value, domain=host, path=path)
                data = self.request("POST", REFRESH)
                # The cabinet rotates refresh_token. Save it before any further reads.
                refresh = data.get("tokens", {}).get("refresh_token")
                if isinstance(refresh, str) and refresh:
                    for cookie in list(self.client.cookies.jar):
                        if cookie.name == "refresh_token":
                            self.client.cookies.delete(
                                cookie.name, domain=cookie.domain, path=cookie.path
                            )
                    self.client.cookies.set(
                        "refresh_token", refresh, domain=".timeweb.cloud", path="/"
                    )
                save(
                    [
                        (c.domain, c.name, c.path, c.value)
                        for c in self.client.cookies.jar
                        if c.domain in HOSTS and c.name in NAMES
                    ]
                )
        else:
            data = self.request(
                "POST",
                LOGIN,
                json={},
                auth=httpx.BasicAuth(
                    required("TIMEWEB_CLOUD_LOGIN"), required("TIMEWEB_CLOUD_PASSWORD")
                ),
            )
        if "two_factor_token" in data or "portal_token" in data:
            raise CheckError(
                "Cloud требует дополнительный вход; войдите через Chromium"
            )
        token = data.get("tokens", {}).get("access_token")
        if not isinstance(token, str) or not token:
            raise CheckError("Cloud не вернул токен сессии; проверьте вход в Chromium")
        self.client.headers["Authorization"] = "Bearer " + token

    def json(self, path):
        return self.request("GET", path)


def run(report, config, now):
    with Cloud() as client:
        client.login()

        def balance():
            data = client.json(FINANCES)["finances"]
            if data["currency"] != "RUB":
                raise CheckError("Неожиданная валюта баланса Cloud")
            value = number(data["balance"])
            monthly = number(data["monthly_cost"])
            hourly = number(data["hourly_cost"])
            if monthly < 0 or hourly < 0:
                raise CheckError("Cloud вернул отрицательную стоимость услуг")
            report.add(SERVICE, "Баланс", rub(value), "WARN" if value <= 0 else "OK")
            report.add(SERVICE, "Расход", f"{rub(monthly)}/мес.; {rub(hourly)}/час")
            if hourly > 0:
                hours = number(data["hours_left"])
                if hours < 0 or hours > 24 * 366 * 100:
                    raise CheckError("Неожиданный остаток времени Cloud")
                until = now + timedelta(hours=float(hours))
                report.add(
                    SERVICE,
                    "Запас средств",
                    f"Примерно {hours / 24:.1f} дн., до {until:%d.%m.%Y %H:%M %Z}; "
                    "оценка Cloud при текущем расходе",
                    "WARN" if hours <= 24 * 30 else "OK",
                )

        def news():
            count = client.json(NEWS)["unread_news_count"]
            if type(count) is not int or count < 0:
                raise CheckError("Изменился формат счётчика новостей Cloud")
            report.add(
                SERVICE, "Новости", f"Непрочитанных: {count}", "WARN" if count else "OK"
            )

        def notifications():
            combined = {}
            for path in (LEGACY_NOTIFICATIONS, NOTIFICATIONS):
                data = client.json(path)
                entries = records(data["notifications"])
                total = data["meta"][
                    "count" if path == LEGACY_NOTIFICATIONS else "total"
                ]
                if type(total) is not int or total != len(entries):
                    raise CheckError("Cloud вернул неполный список уведомлений")
                for entry in entries:
                    if type(entry["id"]) is not int or not isinstance(
                        entry["message"], str
                    ):
                        raise CheckError("Изменился формат уведомлений Cloud")
                    combined[entry["id"]] = entry
            texts = [
                BeautifulSoup(e["message"], "html.parser").get_text(" ", strip=True)[
                    :220
                ]
                for e in list(combined.values())[:3]
            ]
            report.add(
                SERVICE,
                "Уведомления",
                f"Баннеров кабинета: {len(combined)}"
                + ("; " + " | ".join(texts) if texts else ""),
                "WARN" if combined else "OK",
            )

        def status():
            data = client.json(STATUS)["status"]
            flags = [data["is_blocked"], data["is_permanent_blocked"]]
            if not all(type(flag) is bool for flag in flags):
                raise CheckError("Изменился формат статуса аккаунта Cloud")
            report.add(
                SERVICE,
                "Аккаунт",
                "Заблокирован" if any(flags) else "Активен",
                "WARN" if any(flags) else "OK",
            )

        for check, operation in (
            ("Баланс", balance),
            ("Новости", news),
            ("Уведомления", notifications),
            ("Аккаунт", status),
        ):
            report.attempt(SERVICE, check, operation)
