from datetime import UTC, datetime

from .core import Api, CheckError, number, records, required, rub
from .forecast import Subscription, forecast

SERVICE = "SmartVhod"


def flag(value):
    if value in (True, 1, "1", "true"):
        return True
    if value in (False, 0, "0", "false"):
        return False
    return None


def choose_price(options, period, selected_id=None):
    candidates = [
        p
        for p in options
        if (
            str(p["id"]) == str(selected_id)
            if selected_id is not None
            else p["days"] == period
        )
    ]
    if len(candidates) != 1:
        raise CheckError(
            "Тариф неоднозначен или отсутствует; задайте price_ids в config.local.json"
        )
    price = candidates[0]
    if number(price["price"]) <= 0 or int(price["days"]) <= 0:
        raise CheckError("Некорректная цена или период тарифа")
    return price


def normalize_prices(prices, kind):
    result = []
    for p in records(prices):
        eligible = (
            (p.get("protocol") == "MULTI" or p.get("type") == "multi_subscription")
            if kind == "multi"
            else (p.get("type") == "fixed_period" and flag(p.get("is_active")) is True)
        )
        if not eligible or flag(p.get("is_active", True)) is False:
            continue
        regular = p.get("price") if kind == "multi" else p.get("fixed_price")
        if regular is None:
            regular = p.get("fixed_price")
        price = regular
        if (
            flag(p.get("is_promo_active")) is True
            and p.get("promo_fixed_price") is not None
        ):
            price = p["promo_fixed_price"]
        result.append(
            {
                "id": p["id"],
                "days": int(p.get("period_days") or p.get("period")),
                "price": number(price),
            }
        )
    return result


def run(report, config, now):
    login, password = required("SMARTVHOD_LOGIN"), required("SMARTVHOD_PASSWORD")
    with Api("https://smartvhod.ru") as api:
        auth = api.request(
            "/api/auth/login",
            login=True,
            json={"telegram_id": login, "password": password},
        )
        if not isinstance(auth.get("token"), str) or not auth["token"]:
            raise CheckError("SmartVhod не выдал токен авторизации")
        api.client.headers["Authorization"] = f"Bearer {auth['token']}"

        def get_balance():
            value = number(api.request("/api/user/profile")["balance"])
            report.add(SERVICE, "Баланс", rub(value), "WARN" if value < 0 else "OK")
            return value

        balance = report.attempt(SERVICE, "Баланс", get_balance)
        keys = report.attempt(
            SERVICE, "Ключи", lambda: records(api.request("/api/my-keys"))
        )
        multi = report.attempt(
            SERVICE,
            "Multi-VPN",
            lambda: records(api.request("/api/multi/my-subscriptions")),
        )
        subscriptions = []
        reserves = []
        device_total = 0
        unknown_devices = 0
        complete = balance is not None and keys is not None and multi is not None
        prices = report.attempt(
            SERVICE, "Тарифы", lambda: records(api.request("/api/prices"))
        )
        complete = complete and prices is not None
        if keys is not None and multi is not None:
            report.add(
                SERVICE, "Подписки", f"Ключей: {len(keys)}; Multi-VPN: {len(multi)}"
            )
        for kind, entries in (("key", keys), ("multi", multi)):
            for entry in entries or []:
                # Namespace identifiers to avoid collisions between key and multi IDs.
                ident = f"{kind}:{entry['id']}"
                try:
                    item_id = int(entry["id"])
                    old = kind == "key" and entry.get("platform") == "old"
                    expiry = entry.get("expiry_time" if old else "expire_at")
                    if not expiry:
                        raise CheckError(
                            "Нет даты окончания; бессрочный/трафиковый тариф требует отдельного бюджета"
                        )
                    due = (
                        datetime.fromtimestamp(expiry / 1000, UTC)
                        if isinstance(expiry, (int, float))
                        else datetime.fromisoformat(str(expiry))
                    )
                    if due.tzinfo is None:
                        raise CheckError(
                            "Дата без часового пояса; нужно уточнить формат API"
                        )
                    auto = flag(entry.get("auto_renew"))
                    report.add(
                        SERVICE,
                        ident,
                        f"До {due.astimezone(now.tzinfo):%d.%m.%Y %H:%M}; "
                        + (
                            "автопродление включено"
                            if auto is True
                            else "нужно ручное продление"
                            if auto is False
                            else "автопродление неизвестно"
                        ),
                        "WARN" if auto is not True or due <= now else "OK",
                    )

                    if kind == "key" and not old:
                        options = api.request(f"/api/keys/{item_id}/renewal-options")
                        options = records(options["prices"])
                    else:
                        options = normalize_prices(prices, "old" if old else "multi")
                    price = choose_price(
                        options,
                        int(config.get("renewal_days", 30)),
                        config.get("price_ids", {}).get(ident),
                    )
                    subscriptions.append(
                        Subscription(
                            ident, due, number(price["price"]), int(price["days"]), auto
                        )
                    )
                    regular_prices = [
                        p for p in (prices or []) if str(p["id"]) == str(price["id"])
                    ]
                    if len(regular_prices) != 1:
                        raise CheckError(
                            "Не найдена обычная цена выбранного тарифа для резервного прогноза"
                        )
                    regular = number(regular_prices[0]["fixed_price"])
                    reserves.append(
                        Subscription(
                            ident,
                            due,
                            max(regular, number(price["price"])),
                            int(price["days"]),
                            auto,
                        )
                    )
                    report.add(
                        SERVICE,
                        f"{ident}: тариф",
                        f"{rub(price['price'])} / {price['days']} дн. "
                        f"(для прогноза, тариф {price['id']})",
                    )
                except (CheckError, KeyError, TypeError, ValueError) as exc:
                    complete = False
                    report.add(
                        SERVICE,
                        ident,
                        str(exc)
                        if isinstance(exc, CheckError)
                        else "Формат подписки изменился; прогноз неполный",
                        "ERROR",
                    )

                def devices(kind=kind, entry=entry, ident=ident):
                    nonlocal device_total, unknown_devices
                    if kind == "multi":
                        data = records(
                            api.request(f"/api/multi/devices/{int(entry['id'])}")
                        )
                        limit = None
                    elif entry.get("platform") == "old":
                        unknown_devices += 1
                        report.add(
                            SERVICE,
                            f"{ident}: устройства",
                            "Для старых ключей API устройств не найден",
                            "WARN",
                        )
                        return
                    else:
                        result = api.request(f"/api/keys/{int(entry['id'])}/devices")
                        data = records(result["devices"])
                        limit = result.get("deviceLimit")
                    labels = [
                        str(d.get("platform", "?"))
                        + " "
                        + str(d.get("deviceModel") or d.get("model") or "?")
                        for d in data
                    ]
                    device_total += len(data)
                    report.add(
                        SERVICE,
                        f"{ident}: устройства",
                        f"{len(data)}"
                        + (f" / {limit}" if limit is not None else "")
                        + (
                            ": " + "; ".join(labels)
                            if labels
                            else " — нет подключённых"
                        ),
                        "WARN"
                        if limit is not None and len(data) > int(limit)
                        else "OK",
                    )

                errors_before = sum(f.status == "ERROR" for f in report.findings)
                report.attempt(SERVICE, f"{ident}: устройства", devices)
                if sum(f.status == "ERROR" for f in report.findings) > errors_before:
                    unknown_devices += 1

        report.add(
            SERVICE,
            "Устройства",
            f"Получено записей: {device_total}; "
            f"подписок без данных устройств: {unknown_devices}",
            "WARN" if unknown_devices else "OK",
        )

        if not complete:
            report.add(
                SERVICE,
                "Прогноз",
                "Расчёт неполный: не все балансы, подписки или тарифы получены",
                "ERROR",
            )
            return
        result = forecast(
            balance,
            subscriptions,
            now,
            int(config.get("horizon_days", 30)),
            int(config.get("lead_days", 3)),
        )
        details = f"До {result.end:%d.%m.%Y}: продления {rub(result.total)}; пополнить {rub(result.top_up)}"
        if result.deadline:
            details += f" до {result.deadline:%d.%m.%Y %H:%M}; первое непокрытое списание {result.first_shortfall:%d.%m.%Y %H:%M}"
        else:
            details += "; баланса хватает при выбранных тарифах"
        report.add(SERVICE, "Прогноз", details, "WARN" if result.top_up else "OK")
        reserve = forecast(
            balance,
            reserves,
            now,
            int(config.get("horizon_days", 30)),
            int(config.get("lead_days", 3)),
        )
        if reserve.top_up > result.top_up:
            deadline = (
                f" до {reserve.deadline:%d.%m.%Y %H:%M}" if reserve.deadline else ""
            )
            report.add(
                SERVICE,
                "Запас без акции",
                f"Продления {rub(reserve.total)}; "
                f"рекомендуется пополнить {rub(reserve.top_up)}{deadline}",
                "WARN",
            )
        if config.get("details"):
            for due, name, price in result.events:
                report.add(
                    SERVICE,
                    "План продлений",
                    f"{due:%d.%m.%Y %H:%M} — {name}: {rub(price)}",
                )
        report.add(
            SERVICE,
            "Условия прогноза",
            "Сохранение всех подписок, включая ручное продление; "
            "цены на момент обхода. Тариф автосписания нужно сверить с кабинетом.",
            "WARN" if subscriptions else "OK",
        )
