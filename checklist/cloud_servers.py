"""Server status and latest resource measurements from the Cloud dashboard."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .core import CheckError, number, records

SERVERS = "/api/v1/servers"
MAX_AGE = timedelta(minutes=30)
SERVICE = "Timeweb Cloud"


def is_statistics_read(path):
    return bool(
        re.fullmatch(
            r"/api/v3/servers/[1-9][0-9]*/statistics|"
            r"/api/v1/servers/[1-9][0-9]*/statistics/"
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}%3A[0-9]{2}%3A[0-9]{2}Z/1/system\.cpu\.util",
            path,
        )
    )


def cpu_path(server_id, now):
    end = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{SERVERS}/{server_id}/statistics/{quote(end, safe='')}/1/system.cpu.util"


def percent(used, total=100):
    used, total = number(used), number(total)
    if total <= 0 or not 0 <= used <= total:
        raise CheckError("Некорректная метрика: ожидается значение от 0 до 100%")
    return used / total * 100


def latest(entries, time_key, now):
    entries = records(entries)
    if not entries:
        return None
    dated = []
    for entry in entries:
        timestamp = datetime.fromisoformat(entry[time_key])
        if timestamp.tzinfo is None or timestamp > now + timedelta(minutes=5):
            raise CheckError("Некорректное время метрики Cloud")
        dated.append((timestamp, entry))
    return max(dated, key=lambda item: item[0])


def measurement(report, check, entries, time_key, now, calculate, actual=True):
    sample = latest(entries, time_key, now)
    if sample is None:
        report.add(SERVICE, check, "Нет данных мониторинга", "WARN")
        return
    timestamp, entry = sample
    value = calculate(entry)
    if value is None:
        report.add(SERVICE, check, "Последний замер не содержит значения", "WARN")
        return
    stale = not actual or now - timestamp > MAX_AGE
    detail = f"{value:.2f}% · замер {timestamp.astimezone(now.tzinfo):%d.%m %H:%M %Z}"
    if value > 50:
        detail += " · выше 50%"
    if stale:
        detail += " · данные устарели"
    report.add(SERVICE, check, detail, "WARN" if value > 50 or stale else "OK")


def list_servers(client):
    servers, seen = [], set()
    while True:
        data = client.json(SERVERS, params={"limit": 100, "offset": len(servers)})
        page = records(data["servers"])
        total = data["meta"]["total"]
        if type(total) is not int or total < 0:
            raise CheckError("Некорректное количество серверов Cloud")
        for server in page:
            sid = server["id"]
            if type(sid) is not int or sid <= 0 or sid in seen:
                raise CheckError("Некорректный или повторный ID сервера Cloud")
            seen.add(sid)
            servers.append(server)
        if len(servers) == total:
            return servers
        if not page or len(servers) > total:
            raise CheckError("Cloud вернул неполный или изменившийся список серверов")


def check_server(report, client, server, now):
    sid = server["id"]
    name = server["name"]
    if not isinstance(name, str) or not name:
        raise CheckError("Не найдено имя сервера Cloud")
    label = f"{name} (#{sid})"

    def status():
        state, blocked = server["status"], server["is_blocked"]
        if not isinstance(state, str) or not state or type(blocked) is not bool:
            raise CheckError("Изменился формат состояния сервера Cloud")
        report.add(
            SERVICE,
            label + " · Состояние",
            state + (" · заблокирован" if blocked else ""),
            "OK" if state == "on" and not blocked else "WARN",
        )

    def cpu():
        series = records(client.json(cpu_path(sid, now))["statistics"])
        selected = [s for s in series if s["name"] == "system.cpu.util"]
        if len(selected) > 1:
            raise CheckError("Неоднозначная метрика CPU Cloud")
        measurement(
            report,
            label + " · CPU",
            selected[0]["list"] if selected else [],
            "time",
            now,
            lambda e: None if e["value"] is None else percent(e["value"]),
        )

    def ram():
        data = client.json(
            f"/api/v3/servers/{sid}/statistics",
            params={
                "date_from": (now - timedelta(hours=1)).astimezone(UTC).isoformat(),
                "date_to": now.astimezone(UTC).isoformat(),
            },
        )["ram"]
        if type(data["is_actual"]) is not bool:
            raise CheckError("Изменился признак актуальности RAM Cloud")
        measurement(
            report,
            label + " · RAM",
            data["statistic"],
            "logged_at",
            now,
            lambda e: (
                None
                if e["used"] is None or e["total"] is None
                else percent(e["used"], e["total"])
            ),
            actual=data["is_actual"],
        )

    def disks():
        entries = records(server["disks"])
        if not entries:
            report.add(SERVICE, label + " · ROM", "Нет данных о дисках", "WARN")
        for disk in entries:

            def usage(disk=disk):
                disk_id = disk["id"]
                if type(disk_id) is not int or disk_id <= 0:
                    raise CheckError("Некорректный ID диска Cloud")
                check = label + f" · ROM #{disk_id}"
                if disk["used"] is None:
                    report.add(SERVICE, check, "Нет данных о занятом месте", "WARN")
                    return
                value = percent(disk["used"], disk["size"])
                report.add(
                    SERVICE,
                    check,
                    f"{value:.2f}% занято" + (" · выше 50%" if value > 50 else ""),
                    "WARN" if value > 50 else "OK",
                )

            report.attempt(SERVICE, label + " · ROM", usage)

    for metric, operation in (
        ("Состояние", status),
        ("CPU", cpu),
        ("RAM", ram),
        ("ROM", disks),
    ):
        report.attempt(SERVICE, label + " · " + metric, operation)


def run(report, client, now):
    servers = list_servers(client)
    if not servers:
        report.add(SERVICE, "Серверы", "Серверов нет")
    for server in servers:
        report.attempt(
            SERVICE,
            f"Сервер #{server['id']}",
            lambda server=server: check_server(report, client, server, now),
        )
