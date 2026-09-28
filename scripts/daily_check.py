"""Daily Telegram digest; terminal users still have the original rich/JSON CLI."""

import json
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
COMMAND = [str(ROOT / ".venv/bin/python"), "-m", "checklist", "--json", "--direct-interface", "enp9s0"]


def summarize(report):
    findings = report["findings"]
    day = datetime.fromisoformat(report["checked_at"]).astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
    code = report["exit_code"]
    label = (
        "проверка неполная" if code == 2 or not findings else
        "есть предупреждения" if code else
        "есть отложенные проверки" if any(f["status"] == "SKIP" for f in findings) else
        "всё проверено, замечаний нет"
    )
    lines = [f"Обход · {day} МСК — {label}."]
    for service in dict.fromkeys(f["service"] for f in findings):
        items = [f for f in findings if f["service"] == service]
        counts = Counter(f["status"] for f in items)
        status = "ERROR" if counts["ERROR"] else "WARN" if counts["WARN"] else "SKIP" if counts["SKIP"] else "OK"
        lines.append(f"{service}: {status}")
        for f in items:
            check = f["check"]
            if "устройства" in check.casefold() and check.casefold() != "устройства":
                # Per-subscription device findings can contain private labels, even on errors.
                continue
            if f["status"] in {"WARN", "ERROR"} or check in {"Баланс", "Аккаунт"} or check.endswith("· Состояние"):
                lines.append(f"  {check}: {f['detail'][:240]}")
    if not findings:
        lines.append("Нет результатов: проверка не выполнена.")
    if report.get("retried"):
        lines.append("Восстановились после повторной попытки: " + ", ".join(report["retried"]))
    return "\n".join(lines)


def retry_transient(report):
    """Retry only failed read-only cabinets; never consume Cloud's rotating refresh token twice."""
    for service, slug in (("Timeweb Hosting", "hosting"), ("SmartVhod", "smartvhod"), ("CRM", "crm")):
        errors = [f for f in report["findings"] if f["service"] == service and f["status"] == "ERROR"]
        def network_error(f):
            return f["detail"].startswith(("Таймаут", "Ошибка сети"))

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
