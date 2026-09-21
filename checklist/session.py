"""Password login followed by explicitly allowlisted reads. No page JS is run."""

from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .core import CheckError, required


class Cabinet:
    def __init__(self, origin, reads, transport=None):
        self.origin = origin
        self.reads = frozenset(reads)
        self.read_urls = frozenset(urljoin(origin, path) for path in reads)
        self.client = httpx.Client(
            base_url=origin, timeout=25, follow_redirects=False, transport=transport
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def _request(self, method, path, **kwargs):
        url = urlsplit(urljoin(self.origin, path))
        if method not in {"GET", "POST"}:
            raise CheckError("Операции изменения запрещены")
        gateway_read = method == "GET" and url.geturl() in self.read_urls
        if url.scheme != "https" or (
            url.netloc != urlsplit(self.origin).netloc and not gateway_read
        ):
            raise CheckError("Запрос за пределы кабинета заблокирован")
        if method == "POST" and url.path != "/login":
            raise CheckError("Разрешена только отправка формы входа")
        if method == "GET" and not gateway_read and url.path not in {"/login", "/"}:
            raise CheckError("Запрос отсутствует в списке разрешённых чтений")
        try:
            r = self.client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise CheckError("Таймаут кабинета (25 с)") from None
        except httpx.RequestError:
            raise CheckError("Ошибка сети, DNS или TLS") from None
        if r.status_code >= 400:
            raise CheckError(f"HTTP {r.status_code}; проверьте доступ к кабинету")
        return r

    def get(self, path, **kwargs):
        r = self._request("GET", path, **kwargs)
        if r.is_redirect:
            raise CheckError("Кабинет перенаправил запрос: возможно, сессия истекла")
        return r

    def json(self, path, **kwargs):
        try:
            data = self.get(path, **kwargs).json()
        except ValueError:
            raise CheckError(
                "Ожидался JSON; получена страница входа или изменился формат"
            ) from None
        if isinstance(data, dict) and (
            data.get("error") or data.get("success") is False
        ):
            raise CheckError("Кабинет вернул ошибку")
        return data

    def login_crm(self):
        soup = BeautifulSoup(self.get("/login").text, "html.parser")
        csrf = soup.select_one('input[name="_csrf_token"]')
        if csrf is None:
            raise CheckError("Не найдена форма входа CRM")
        r = self._request(
            "POST",
            "/login",
            data={
                "_csrf_token": csrf["value"],
                "email": required("ASPRO_LOGIN"),
                "password": required("ASPRO_PASSWORD"),
            },
        )
        for _ in range(5):
            if not r.is_redirect:
                break
            target = r.headers.get("location", "")
            u = urlsplit(urljoin(self.origin, target))
            if u.path != "/" or set(parse_qs(u.query)) - {"_auth_token"}:
                raise CheckError("CRM требует дополнительный шаг входа (2FA/CAPTCHA)")
            r = self._request("GET", target)
        soup = BeautifulSoup(r.text, "html.parser")
        if (
            r.is_redirect
            or soup.select_one("#inputPassword")
            or not soup.select_one("#header_inbox_bar")
        ):
            raise CheckError("Вход CRM не подтверждён; проверьте логин, пароль и 2FA")
        return r.text

    def login_hosting(self):
        soup = BeautifulSoup(self.get("/login").text, "html.parser")
        token = soup.select_one('meta[name="csrf-token"]')
        if token is None:
            raise CheckError("Не найдена форма входа Hosting")
        csrf = token["content"]
        r = self._request(
            "POST",
            "/login",
            json={
                "LoginForm": {
                    "username": required("TIMEWEB_HOSTING_LOGIN"),
                    "password": required("TIMEWEB_HOSTING_PASSWORD"),
                    "_csrf": csrf,
                }
            },
            headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": csrf},
        )
        try:
            data = r.json()
        except ValueError:
            raise CheckError("Неожиданный ответ формы входа Hosting") from None
        if data.get("type") != "success":
            raise CheckError("Hosting отклонил вход или требует CAPTCHA/2FA")
        self.client.headers["X-Requested-With"] = "XMLHttpRequest"
        return self.get("/").text
