from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from checklist.core import CheckError
from checklist.forecast import Subscription, forecast

NOW = datetime(2026, 9, 21, 12, tzinfo=ZoneInfo("Europe/Moscow"))


def sub(name="a", day=5, price="100", period=30, auto=True):
    return Subscription(name, NOW + timedelta(days=day), Decimal(price), period, auto)


def test_multiple_charges_and_top_up_deadline():
    f = forecast(
        "200", [sub("a", 9), sub("b", 9), sub("c", 13), sub("d", 17, "250")], NOW
    )
    assert f.total == Decimal(550)
    assert f.top_up == Decimal(350)
    assert f.first_shortfall == NOW + timedelta(days=13)
    assert f.deadline == NOW + timedelta(days=10)


def test_repeated_charges_and_inclusive_horizon():
    f = forecast("0", [sub(day=0, price="0.10", period=10)], NOW)
    assert len(f.events) == 4
    assert f.total == Decimal("0.40")
    assert f.deadline == NOW


def test_exact_balance_no_shortfall():
    f = forecast("100", [sub()], NOW)
    assert f.top_up == 0
    assert f.deadline is None


def test_expired_manual_renewal_is_due_now():
    f = forecast("0", [sub(day=-40, period=31, auto=False)], NOW)
    assert f.events == ((NOW, "a", Decimal(100)),)
    assert f.deadline == NOW


def test_negative_balance_and_round_up():
    f = forecast("-0.001", [], NOW)
    assert f.top_up == Decimal("0.01")
    assert f.deadline == NOW


def test_output_dates_use_report_timezone():
    s = Subscription(
        "a",
        (NOW + timedelta(days=5)).astimezone(ZoneInfo("UTC")),
        Decimal(10),
        30,
        True,
    )
    f = forecast(0, [s], NOW)
    assert str(f.first_shortfall.tzinfo) == "Europe/Moscow"


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-1", "0"])
def test_invalid_cost_never_succeeds(price):
    with pytest.raises(CheckError):
        forecast(0, [sub(price=price)], NOW)


def test_subscription_beyond_horizon():
    assert forecast(10, [sub(day=31)], NOW).total == 0
