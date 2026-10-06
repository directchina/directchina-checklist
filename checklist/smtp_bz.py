"""Read-only SMTP.BZ cabinet check. Never send messages or expose their contents."""

import html
import re
from html.parser import HTMLParser

import httpx

from .core import CheckError, Finding, number, required, rub

SERVICE = "SMTP.BZ"
ORIGIN = "https://smtp.bz"
LOG = "/panel/pagedata/sentlogs.php"
READS = {"/login", "/panel/", LOG}
FAILED = {"Не существует", "Возвращено", "Отменено", "Ошибка"}
PENDING = {"Отправлено", "Повтор", "В очереди"}
SAMPLE = 100
WARN_PERCENT = 5


class _BillingLink(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.text = ""
        self.matches = []

    def handle_starttag(self, tag, attrs):
        if tag == "a" and dict(attrs).get("href") == "/panel/billing":
            self.inside = True
            self.text = ""

    def handle_data(self, data):
        if self.inside:
            self.text += data

    def handle_endtag(self, tag):
        if tag == "a" and self.inside:
            self.matches.append(self.text.strip())
            self.inside = False


def parse_balance(page):
    links = _BillingLink()
    links.feed(page)
    amounts = [
        re.fullmatch(r"Мои финансы:\s*(-?[\d\s.,]+)\s*(?:р|₽)", text)
        for text in links.matches
    ]
    if len(amounts) != 1 or amounts[0] is None:
        raise CheckError("Не удалось однозначно прочитать баланс SMTP.BZ")
    return number(amounts[0][1])


def parse_sends(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise CheckError("Журнал SMTP.BZ вернул неожиданный формат")
    rows = data["data"]
    if (not isinstance(data.get("recordsFiltered"), int)
            or data["recordsFiltered"] < len(rows)
            or len(rows) > SAMPLE):
        raise CheckError("Журнал SMTP.BZ вернул противоречивую страницу")
    failed = delivered = pending = 0
    for row in rows:
        if not isinstance(row, list) or len(row) != 8 or not isinstance(row[6], str):
            raise CheckError("Строка журнала SMTP.BZ изменила формат")
        status = html.unescape(row[6].split("<", 1)[0]).strip()
        if status in FAILED:
            failed += 1
        elif status == "Доставлено":
            delivered += 1
        elif status in PENDING:
            pending += 1
        else:
            raise CheckError("Неизвестный статус отправки SMTP.BZ; проверьте адаптер")
    total = len(rows)
    if not total:
        return Finding(SERVICE, "Последние отправки", "OK", "В журнале пока нет отправок")
    ratio = failed * 100 / total
    detail = (f"Ошибки доставки: {failed} из {total} ({ratio:g}%); "
              f"доставлено: {delivered}")
    if pending:
        detail += f"; в обработке/повторе: {pending}"
    detail += f". Предупреждение от {WARN_PERCENT}%."
    return Finding(SERVICE, "Последние отправки",
                   "WARN" if ratio >= WARN_PERCENT else "OK", detail)


def check(login, password, *, transport=None):
    """Authenticate, then read only the balance link and newest 100 journal rows."""
    with httpx.Client(base_url=ORIGIN, timeout=20, follow_redirects=False,
                      transport=transport) as client:
        def request(method, path, **kwargs):
            if (path not in READS or (method == "POST") != (path == "/login" and
                    "data" in kwargs)):
                raise CheckError("SMTP.BZ: запрещённый маршрут или метод")
            try:
                response = client.request(method, path, **kwargs)
            except httpx.TimeoutException:
                raise CheckError("Таймаут SMTP.BZ (20 с)") from None
            except httpx.RequestError:
                raise CheckError("Ошибка сети, DNS или TLS SMTP.BZ") from None
            if not 200 <= response.status_code < 400:
                raise CheckError(f"SMTP.BZ вернул HTTP {response.status_code}")
            return response

        if request("GET", "/login").status_code != 200:
            raise CheckError("Не открылась страница входа SMTP.BZ")
        auth = request("POST", "/login", data={"email": login, "password": password})
        if auth.status_code != 302 or auth.headers.get("location") != "/panel/":
            raise CheckError("SMTP.BZ отклонил вход или запросил дополнительную проверку")
        page = request("GET", "/panel/")
        if page.status_code != 200:
            raise CheckError("Не удалось открыть кабинет SMTP.BZ")
        balance = parse_balance(page.text)
        log = request("GET", LOG, params={"draw": 1, "start": 0, "length": SAMPLE})
        if log.status_code != 200:
            raise CheckError("Не удалось прочитать журнал SMTP.BZ")
        try:
            data = log.json()
        except ValueError:
            raise CheckError("Журнал SMTP.BZ вернул не JSON") from None
        return [Finding(SERVICE, "Баланс", "WARN" if balance <= 0 else "OK",
                        rub(balance)), parse_sends(data)]


def run(report, config, now, *, transport=None):
    login, password = required("SMTPBZ_LOGIN"), required("SMTPBZ_PASS")
    for finding in check(login, password, transport=transport):
        report.add(finding.service, finding.check, finding.detail, finding.status)
