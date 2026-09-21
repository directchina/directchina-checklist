import json
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .core import CheckError, number, records, rub
from .session import Cabinet

SERVICE = "Timeweb Hosting"
NEWS = "/api/v1.1/news"
UNREAD = "/api/v1.1/counters/news"
CLOSED = "/site/get-notifications-closed"
NOTIFICATIONS = "https://hosting-gw.timeweb.ru/gw/hosting/v1/notifications"


def run(report, config, now):
    with Cabinet(
        "https://hosting.timeweb.ru", [NEWS, UNREAD, CLOSED, NOTIFICATIONS]
    ) as client:
        html = client.login_hosting()
        match = re.search(r"window.Timeweb.config\s*=\s*({.*?});", html)
        if not match:
            raise CheckError("Не найдены данные аккаунта после входа")
        account = json.loads(match[1])["USERID"]
        if not re.fullmatch(r"[a-zA-Z0-9_]+", account):
            raise CheckError("Неожиданный идентификатор аккаунта")
        finances = f"/api/v2/finances/accounts/{account}"
        client.read_urls |= {client.origin + finances}

        def balance():
            data = client.json(finances)
            if data["currency"] != "RUB":
                raise CheckError("Неожиданная валюта баланса")
            value = number(data["balance"])
            report.add(SERVICE, "Баланс", rub(value), "WARN" if value <= 0 else "OK")

        def news():
            unread = client.json(UNREAD)
            if not isinstance(unread, list) or not all(
                isinstance(x, int) for x in unread
            ):
                raise CheckError("Изменился формат списка непрочитанных новостей")
            if not unread:
                report.add(SERVICE, "Новости", "Непрочитанных новостей нет")
                return
            entries = records(client.json(NEWS))
            selected = [entry for entry in entries if entry["id"] in unread]
            if set(unread) - {entry["id"] for entry in selected}:
                raise CheckError("Не все непрочитанные новости найдены в списке")
            selected.sort(key=lambda x: x["date"], reverse=True)
            titles = [
                str(e["date"])[:10] + ": " + str(e["caption"]) for e in selected[:3]
            ]
            report.add(
                SERVICE,
                "Новости",
                f"Непрочитанных: {len(unread)}; " + " | ".join(titles),
                "WARN",
            )

        def notifications():
            xsrf = [
                c.value
                for c in client.client.cookies.jar
                if c.name == "xsrf" and c.domain.lstrip(".") == "timeweb.ru"
            ]
            if len(xsrf) != 1:
                raise CheckError("Не найдена однозначная XSRF-cookie шлюза Hosting")
            data = client.json(
                NOTIFICATIONS, headers={"X-XSRF-TOKEN": unquote(xsrf[0])}
            )
            entries = records(data["notifications"])
            closed = client.json(CLOSED)
            if not isinstance(closed, list) or not all(
                isinstance(x, int) for x in closed
            ):
                raise CheckError("Изменился формат закрытых уведомлений")
            active = [e for e in entries if e.get("id") not in closed]
            texts = []
            for e in active[:3]:
                raw = (
                    e.get("title")
                    or e.get("text")
                    or e.get("message")
                    or e.get("caption")
                )
                texts.append(
                    BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)[:220]
                    if isinstance(raw, str)
                    else f"Уведомление #{e.get('id', '?')}"
                )
            report.add(
                SERVICE,
                "Уведомления",
                f"Незакрытых системных баннеров: {len(active)}"
                + ("; " + " | ".join(texts) if texts else ""),
                "WARN" if active else "OK",
            )

        report.attempt(SERVICE, "Баланс", balance)
        report.attempt(SERVICE, "Новости", news)
        report.attempt(SERVICE, "Уведомления", notifications)
