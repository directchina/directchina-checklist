from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from checklist import smartvhod
from checklist.core import CheckError, Report


def test_tariff_selection_rejects_ambiguity():
    options = [{"id": 1, "days": 30, "price": 100}, {"id": 2, "days": 30, "price": 125}]
    with pytest.raises(CheckError):
        smartvhod.choose_price(options, 30)
    assert smartvhod.choose_price(options, 30, selected_id=2)["price"] == 125


def test_promo_and_inactive_tariffs():
    data = [
        {
            "id": 1,
            "type": "fixed_period",
            "period_days": 30,
            "fixed_price": 125,
            "promo_fixed_price": 100,
            "is_promo_active": True,
            "is_active": True,
        },
        {
            "id": 2,
            "type": "fixed_period",
            "period_days": 30,
            "fixed_price": 1,
            "is_active": False,
        },
    ]
    assert smartvhod.normalize_prices(data, "old") == [
        {"id": 1, "days": 30, "price": Decimal(100)}
    ]


def test_old_timestamp_and_failed_subscription_prevent_green_forecast(monkeypatch):
    now = datetime(2026, 9, 21, 12, tzinfo=ZoneInfo("Europe/Moscow"))
    monkeypatch.setenv("SMARTVHOD_LOGIN", "example-user")
    monkeypatch.setenv("SMARTVHOD_PASSWORD", "example-password")

    class FakeApi:
        def __init__(self, *args):
            self.client = type("Client", (), {"headers": {}})()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, path, **kwargs):
            data = {
                "/api/auth/login": {"token": "private-session"},
                "/api/user/profile": {"balance": 100},
                "/api/my-keys": [
                    {
                        "id": 1,
                        "platform": "old",
                        "auto_renew": 1,
                        "expiry_time": int(now.timestamp() * 1000),
                    },
                    {"id": 2, "platform": "old", "auto_renew": 1, "expiry_time": None},
                ],
                "/api/multi/my-subscriptions": [],
                "/api/prices": [
                    {
                        "id": 6,
                        "type": "fixed_period",
                        "period_days": 30,
                        "fixed_price": 125,
                        "is_active": True,
                    }
                ],
            }
            return data[path]

    monkeypatch.setattr(smartvhod, "Api", FakeApi)
    report = Report()
    smartvhod.run(report, {}, now)
    assert any(f.check == "key:1: тариф" and f.status == "OK" for f in report.findings)
    assert any(f.check == "Прогноз" and f.status == "ERROR" for f in report.findings)
    assert not any(f.check == "Прогноз" and f.status == "OK" for f in report.findings)
