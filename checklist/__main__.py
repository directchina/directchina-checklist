import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import crm, smartvhod, timeweb_cloud, timeweb_hosting
from .core import Report

ROOT = Path(__file__).resolve().parent.parent
SERVICES = {
    "hosting": ("Timeweb Hosting", timeweb_hosting.run),
    "smartvhod": ("SmartVhod", smartvhod.run),
    "crm": ("CRM", crm.run),
    "cloud": ("Timeweb Cloud", timeweb_cloud.run),
}


def render(report, now, details=False):
    console = Console()
    console.print(Text(f"Сисадминский обход · {now:%d.%m.%Y %H:%M %Z}", style="bold"))
    console.print(
        "Только чтение · CRM: системные новости · SmartVhod: прогноз продлений",
        style="dim",
    )
    table = Table(show_lines=True, expand=True)
    for title in ("Сервис", "Статус", "Результат"):
        table.add_column(title, no_wrap=title in ("Сервис", "Статус"))
    styles = {"OK": "green", "WARN": "yellow", "ERROR": "bold red", "SKIP": "dim"}
    labels = {"OK": "OK", "WARN": "WARN", "ERROR": "ERROR", "SKIP": "SKIP"}
    for f in report.findings:
        if not details and f.status == "OK" and f.check.startswith(("key:", "multi:")):
            continue
        table.add_row(
            Text(f.service),
            Text(labels[f.status], style=styles[f.status]),
            Text.assemble((f.check + ": ", "bold"), f.detail),
        )
    console.print(table)
    errors = sum(f.status == "ERROR" for f in report.findings)
    console.print(
        Text(
            f"Ошибок: {errors} · код завершения: {report.exit_code}",
            style="red" if errors else "dim",
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Обход кабинетов: только авторизация и чтение"
    )
    parser.add_argument("--service", choices=list(SERVICES) + ["all"], default="all")
    parser.add_argument(
        "--details",
        action="store_true",
        help="Все подписки, устройства и план продлений",
    )
    parser.add_argument(
        "--json", action="store_true", help="JSON в stdout вместо таблицы"
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.json")
    args = parser.parse_args(argv)
    report = Report()
    now = datetime.now().astimezone()
    try:
        load_dotenv(args.env_file, interpolate=False)
        now = datetime.now(
            ZoneInfo(os.environ.get("CHECKLIST_TIMEZONE", "Europe/Moscow"))
        )
        config = json.loads(args.config.read_text()) if args.config.exists() else {}
        if not isinstance(config, dict):
            raise TypeError("Config must be an object")
        vpn = dict(config.get("smartvhod", {}))
        vpn.update(
            horizon_days=int(os.environ.get("SMARTVHOD_HORIZON_DAYS", "30")),
            lead_days=int(os.environ.get("SMARTVHOD_LEAD_DAYS", "3")),
            details=args.details,
        )
        if not 1 <= vpn["horizon_days"] <= 366 or not 0 <= vpn["lead_days"] <= 365:
            raise ValueError("Invalid horizon")
        config["smartvhod"] = vpn
    except (ValueError, TypeError, OSError, ZoneInfoNotFoundError):
        report.add(
            "Настройки",
            "Конфигурация",
            "Проверьте JSON, часовой пояс, горизонт (1–366) и запас (0–365 дней)",
            "ERROR",
        )
    if not report.findings:
        selected = SERVICES if args.service == "all" else [args.service]
        for key in selected:
            name, run = SERVICES[key]
            report.attempt(
                name,
                "Обход",
                lambda key=key, run=run: run(report, config.get(key, {}), now),
            )
    if args.json:
        print(
            json.dumps(
                {
                    "checked_at": now.isoformat(),
                    "exit_code": report.exit_code,
                    "findings": [asdict(f) for f in report.findings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        render(report, now, args.details)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
