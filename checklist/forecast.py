from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from .core import CheckError, number


@dataclass(frozen=True)
class Subscription:
    name: str
    due: datetime
    price: Decimal
    period_days: int
    auto_renew: bool | None


@dataclass(frozen=True)
class Forecast:
    end: datetime
    total: Decimal
    top_up: Decimal
    first_shortfall: datetime | None
    deadline: datetime | None
    events: tuple


def forecast(balance, subscriptions, now, days=30, lead_days=3):
    """Budget for keeping every listed subscription through the inclusive horizon.

    Expired subscriptions require one immediate renewal, starting a new period now.
    All expirations must be timezone-aware. Disabled autorenew still needs manual funding.
    """
    if now.tzinfo is None or not 1 <= days <= 366 or not 0 <= lead_days <= 365:
        raise CheckError("Неверный горизонт, запас дней или часовой пояс расчёта")
    balance = number(balance)
    end = now + timedelta(days=days)
    events = []
    for sub in subscriptions:
        if (
            sub.due.tzinfo is None
            or not 1 <= sub.period_days <= 3660
            or number(sub.price) <= 0
        ):
            raise CheckError("Некорректная дата, период или стоимость подписки")
        due = max(now, sub.due.astimezone(now.tzinfo))
        while due <= end:
            events.append((due, sub.name, number(sub.price)))
            due += timedelta(days=sub.period_days)
    events.sort(key=lambda item: (item[0], item[1]))
    remaining = balance
    first = now if balance < 0 else None
    for due, _, price in events:
        remaining -= price
        if remaining < 0 and first is None:
            first = due
    total = sum((event[2] for event in events), Decimal(0))
    top_up = max(Decimal(0), total - balance).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING
    )
    deadline = max(now, first - timedelta(days=lead_days)) if first else None
    return Forecast(end, total, top_up, first, deadline, tuple(events))
