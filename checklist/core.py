import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx


class CheckError(Exception):
    """Safe, user-facing error; never include response bodies or credentials."""


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not name.endswith("_PASSWORD"):
        value = value.strip()
    if not value:
        raise CheckError(f"Заполните {name} в .env")
    return value


def number(value) -> Decimal:
    try:
        result = Decimal(
            str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
        )
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise CheckError(
            "Ожидалось конечное числовое значение; формат данных изменился"
        ) from None


def rub(value) -> str:
    return f"{number(value):,.2f} ₽".replace(",", " ")


def clean(value: str) -> str:
    # Prevent terminal escapes and accidental credential disclosure in server labels.
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    for key, secret in os.environ.items():
        if secret and any(
            part in key for part in ("PASSWORD", "TOKEN", "APP_KEY", "LOGIN")
        ):
            value = value.replace(secret, "***")
    return value


@dataclass
class Finding:
    service: str
    check: str
    status: str
    detail: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, service, check, detail, status="OK"):
        self.findings.append(
            Finding(clean(service), clean(check), status, clean(detail))
        )

    def attempt(self, service: str, check: str, operation: Callable):
        try:
            return operation()
        except CheckError as exc:
            self.add(service, check, str(exc), "ERROR")
        except (KeyError, TypeError, ValueError, IndexError):
            self.add(
                service,
                check,
                "Неожиданный формат данных; требуется проверить адаптер",
                "ERROR",
            )
        except Exception as exc:  # noqa: BLE001 — isolate failures, never print secret-bearing exceptions
            self.add(
                service,
                check,
                f"Сбой {type(exc).__name__}; проверка не завершена",
                "ERROR",
            )
        return None

    @property
    def exit_code(self):
        return (
            2
            if any(f.status == "ERROR" for f in self.findings)
            else int(any(f.status == "WARN" for f in self.findings))
        )


class Api:
    def __init__(self, base, headers=None, transport=None):
        self.client = httpx.Client(
            base_url=base,
            headers=headers,
            timeout=20,
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def request(self, path, *, login=False, **kwargs):
        # Only SmartVhod's observed auth and read endpoints are callable.
        if str(self.client.base_url).rstrip("/") != "https://smartvhod.ru":
            raise CheckError("Неизвестный адрес API")
        allowed = (
            path == "/api/auth/login"
            if login
            else bool(
                re.fullmatch(
                    r"/api/(?:user/profile|my-keys|prices|multi/my-subscriptions|multi/devices/\d+|keys/\d+/(?:devices|renewal-options))",
                    path,
                )
            )
        )
        if not allowed or kwargs.get("follow_redirects"):
            raise CheckError(
                "Запрос отсутствует в списке разрешённых чтений/авторизации"
            )
        try:
            response = self.client.request("POST" if login else "GET", path, **kwargs)
        except httpx.TimeoutException:
            raise CheckError("Таймаут запроса (20 с)") from None
        except httpx.RequestError:
            raise CheckError("Ошибка сети, DNS или TLS") from None
        if response.status_code in (401, 403):
            raise CheckError(
                f"HTTP {response.status_code}: вход отклонён или недостаточно прав"
            )
        if not 200 <= response.status_code < 300:
            raise CheckError(f"HTTP {response.status_code}: запрос не выполнен")
        try:
            data = response.json()
        except ValueError:
            raise CheckError(
                "Получен не JSON: возможно, страница входа или защиты"
            ) from None
        if isinstance(data, dict) and (
            data.get("error") or data.get("success") is False
        ):
            raise CheckError(
                "API сообщил об ошибке; проверьте доступ и состояние кабинета"
            )
        return data


def records(data):
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise CheckError("Ожидался список записей; формат API изменился")
    return data
