"""Daily Telegram digest; terminal users still have the original rich/JSON CLI."""

import json
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
COMMAND = [str(ROOT / ".venv/bin/python"), "-m", "checklist", "--json", "--direct-interface", "enp9s0"]


def _status_icon(items):
    counts = Counter(f["status"] for f in items)
    return "❌" if counts["ERROR"] else "⚠️" if counts["WARN"] else "⏭️" if counts["SKIP"] else "✅"


def _issue(finding):
    check, detail = finding["check"], finding["detail"][:240]
    if check == "Прогноз":
        payment = re.search(r"пополнить (.+?) до (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})", detail)
        if payment:
            return f"Пополнить {payment[1]} до {payment[2]}"
    if check == "Запас без акции":
        reserve = re.search(r"рекомендуется пополнить (.+)$", detail)
        if reserve:
            return f"С запасом без акции: {reserve[1]}"
    if check.startswith("multi:") and check.count(":") == 1:
        return f"Multi-VPN #{check.split(':', 1)[1]}: {detail}"
    return f"{check}: {detail}"


def _server_lines(server, items):
    issues = [f for f in items if f["status"] in {"WARN", "ERROR", "SKIP"}]
    state = next((f for f in items if f["check"].endswith("· Состояние")), None)
    if not issues:
        suffix = "включён, ресурсы в норме" if state and state["detail"] == "on" else "без замечаний"
        return [f"  ✅ {server} — {suffix}"]
    lines = [f"  {_status_icon(items)} {server}"]
    for f in issues:
        metric = f["check"].rsplit(" · ", 1)[1].replace("ROM #", "Диск #")
        lines.append(f"    • {metric}: {f['detail'][:240]}")
    return lines


def summarize(report):
    findings = report["findings"]
    day = datetime.fromisoformat(report["checked_at"]).astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M")
    code = report["exit_code"]
    label = (
        "❌ Проверка неполная" if code == 2 or not findings else
        "⚠️ Есть замечания" if code else
        "⏭️ Есть отложенные проверки" if any(f["status"] == "SKIP" for f in findings) else
        "✅ Всё проверено, замечаний нет"
    )
    lines = [f"🧭 Обход систем · {day} МСК", label]
    if not findings:
        return "\n".join([*lines, "", "Нет результатов: проверка не выполнена."])
    for service in dict.fromkeys(f["service"] for f in findings):
        items = [f for f in findings if f["service"] == service]
        section = [f"{_status_icon(items)} {service}"]
        servers = {}
        hidden_device_issue = False
        for f in items:
            check = f["check"]
            if "устройства" in check.casefold() and check.casefold() != "устройства":
                # Do not publish per-device names, including names in failed checks.
                hidden_device_issue |= f["status"] in {"WARN", "ERROR"}
                continue
            if service == "Timeweb Cloud" and " · " in check:
                server, metric = check.rsplit(" · ", 1)
                if metric in {"Состояние", "CPU", "RAM", "ROM"} or metric.startswith("ROM #"):
                    servers.setdefault(server, []).append(f)
                    continue
            if check == "Баланс":
                section.append(f"  Баланс: {f['detail'][:240]}")
            elif service == "SMTP.BZ" and check == "Последние отправки":
                section.append(f"  {f['detail'][:240]}")
            elif check == "Условия прогноза" and f["status"] == "WARN":
                section.append(
                    "  • Прогноз ориентировочный: учтены ручные продления; "
                    "цены на момент проверки; тариф автосписания сверить в кабинете"
                )
            elif service == "CRM" and check == "Системные новости" and f["status"] == "OK":
                section.append("  Непрочитанных системных объявлений нет")
            elif f["status"] in {"WARN", "ERROR", "SKIP"}:
                section.append(f"  • {_issue(f)}")
        if hidden_device_issue:
            section.append("  • Устройства: требуется внимание (подробности в терминале)")
        for server, measurements in servers.items():
            section.extend(_server_lines(server, measurements))
        lines.extend(["", *section])
    if report.get("retried"):
        lines.extend(["", "↻ Восстановились после повторной попытки: " + ", ".join(report["retried"])])
    return "\n".join(lines)


def retry_transient(report):
    """Retry only failed read-only cabinets; never consume Cloud's rotating refresh token twice."""
    for service, slug in (("Timeweb Hosting", "hosting"), ("SmartVhod", "smartvhod"),
                          ("SMTP.BZ", "smtp_bz"), ("CRM", "crm")):
        errors = [f for f in report["findings"] if f["service"] == service and f["status"] == "ERROR"]
        def network_error(f):
            detail = f["detail"]
            return detail.startswith(("Таймаут", "Ошибка сети")) or bool(
                re.match(r"HTTP 50[234](?:\D|$)", detail)
            )

        def dependent_forecast(f, service=service):
            return (service == "SmartVhod" and f["check"] == "Прогноз"
                    and f["detail"].startswith("Расчёт неполный: не все балансы, подписки или тарифы получены"))

        if not any(map(network_error, errors)) or not all(
            network_error(f) or dependent_forecast(f) for f in errors
        ):
            continue
        try:
            cmd = [*COMMAND, "--service", slug]
            result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=90, check=False)
            fresh = json.loads(result.stdout)
            replacements = fresh["findings"]
            if result.returncode != fresh["exit_code"] or not replacements or any(
                f["service"] != service or f["status"] == "ERROR" for f in replacements
            ):
                continue
            if service == "SmartVhod" and not any(
                f["check"] == "Прогноз" and f["status"] in {"OK", "WARN"}
                for f in replacements
            ):
                continue
            if service == "SMTP.BZ" and not {"Баланс", "Последние отправки"} <= {
                f["check"] for f in replacements if f["status"] in {"OK", "WARN"}
            }:
                continue
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            continue  # Keep the original error visible in the daily report.
        old = report["findings"]
        index = next(i for i, f in enumerate(old) if f["service"] == service)
        report["findings"] = [f for f in old[:index] if f["service"] != service] + replacements + [
            f for f in old[index:] if f["service"] != service
        ]
        report.setdefault("retried", []).append(service)
    report["exit_code"] = 2 if any(f["status"] == "ERROR" for f in report["findings"]) else int(
        any(f["status"] == "WARN" for f in report["findings"])
    )
    return report


def main():
    try:
        result = subprocess.run(COMMAND, cwd=ROOT, capture_output=True, text=True, timeout=240, check=False)
        report = json.loads(result.stdout)
        if result.returncode != report["exit_code"]:
            raise ValueError("Код процесса не совпал с отчётом")
        print(summarize(retry_transient(report)))
        # A structured report (even with service errors) is a successful script run:
        # Hermes no-agent delivers stdout only when this process exits zero.
        return 0
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        # Never include stderr/stdout: they may contain credentials or private data.
        print("Обход: не удалось получить отчёт. Проверьте запуск вручную и состояние сети.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
