import json
import subprocess

from scripts import daily_check
from scripts.daily_check import summarize


def test_summary_is_scannable_and_groups_cloud_servers():
    data = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 1, "findings": [
        {"service": "Timeweb Hosting", "check": "Баланс", "status": "OK", "detail": "8 881.09 ₽"},
        {"service": "SmartVhod", "check": "Баланс", "status": "OK", "detail": "300.00 ₽"},
        {"service": "SmartVhod", "check": "Прогноз", "status": "WARN", "detail": "До 28.10.2026: продления 1 200.00 ₽; пополнить 900.00 ₽ до 04.10.2026 22:06; первое непокрытое списание 07.10.2026 22:06"},
        {"service": "SmartVhod", "check": "Запас без акции", "status": "WARN", "detail": "Продления 1 500.00 ₽; рекомендуется пополнить 1 200.00 ₽ до 01.10.2026 09:52"},
        {"service": "SmartVhod", "check": "Условия прогноза", "status": "WARN", "detail": "Длинный технический дисклеймер"},
        {"service": "CRM", "check": "Системные новости", "status": "OK", "detail": "Непрочитанных объявлений сервиса нет"},
        {"service": "Timeweb Cloud", "check": "Баланс", "status": "OK", "detail": "19 362.56 ₽"},
        {"service": "Timeweb Cloud", "check": "Daring Cygnus (#1) · Состояние", "status": "OK", "detail": "on"},
        {"service": "Timeweb Cloud", "check": "Daring Cygnus (#1) · RAM", "status": "WARN", "detail": "Нет данных мониторинга"},
        {"service": "Timeweb Cloud", "check": "Daring Cygnus (#1) · ROM #2", "status": "WARN", "detail": "61.26% занято · выше 50%"},
        {"service": "Timeweb Cloud", "check": "Radicale Backup (#3) · Состояние", "status": "OK", "detail": "on"},
        {"service": "Timeweb Cloud", "check": "Radicale Backup (#3) · CPU", "status": "OK", "detail": "5%"},
    ]}
    text = summarize(data)
    assert text.startswith("🧭 Обход систем · 28.09.2026, 14:00 МСК\n⚠️ Есть замечания")
    assert "\n\n✅ Timeweb Hosting\n" in text
    assert "\n\n⚠️ SmartVhod\n" in text
    assert "Пополнить 900.00 ₽ до 04.10.2026 22:06" in text
    assert "С запасом без акции: 1 200.00 ₽ до 01.10.2026 09:52" in text
    assert "Длинный технический дисклеймер" not in text
    assert "\n\n✅ CRM\n" in text
    assert "  Непрочитанных системных объявлений нет" in text
    assert "\n\n⚠️ Timeweb Cloud\n" in text
    assert "Daring Cygnus (#1)" in text and "Диск #2: 61.26%" in text
    assert "✅ Radicale Backup (#3)" in text


def test_summary_keeps_issues_and_hides_device_names():
    data = {"checked_at": "2026-09-28T14:00:01+03:00", "exit_code": 1, "findings": [
        {"service": "SmartVhod", "check": "key:123: устройства", "status": "OK", "detail": "Laptop SECRETNAME"},
        {"service": "SmartVhod", "check": "Прогноз", "status": "WARN", "detail": "Пополнить 900 ₽"},
        {"service": "Timeweb Cloud", "check": "Server · ROM #1", "status": "WARN", "detail": "61%"},
        {"service": "Timeweb Cloud", "check": "Баланс", "status": "OK", "detail": "100 ₽"},
    ]}
    message = summarize(data)
    assert "Пополнить 900 ₽" in message and "61%" in message
    assert "SECRETNAME" not in message
    assert "100 ₽" in message
    assert "28.09.2026" in message


def test_summary_marks_errors_even_when_other_services_pass():
    data = {"checked_at": "2026-09-28T14:00:01+03:00", "exit_code": 2, "findings": [
        {"service": "CRM", "check": "Обход", "status": "ERROR", "detail": "Таймаут кабинета"},
        {"service": "Timeweb Cloud", "check": "Аккаунт", "status": "OK", "detail": "Активен"},
    ]}
    message = summarize(data)
    assert "CRM" in message and "Таймаут кабинета" in message and "неполная" in message


def test_empty_report_is_never_presented_as_all_clear():
    data = {"checked_at": "2026-09-28T14:00:01+03:00", "exit_code": 0, "findings": []}
    message = summarize(data)
    assert "неполная" in message
    assert "Нет результатов" in message


def test_summary_redacts_failed_device_checks_but_keeps_safe_errors_and_resources():
    data = {"checked_at": "2026-09-28T14:00:01+03:00", "exit_code": 2, "findings": [
        {"service": "SmartVhod", "check": "key:123: устройства PRIVATE-LAPTOP", "status": "ERROR", "detail": "PRIVATE-LAPTOP owner"},
        {"service": "SmartVhod", "check": "multi:456: устройства SECRET-PHONE", "status": "WARN", "detail": "SECRET-PHONE offline"},
        {"service": "SmartVhod", "check": "Обход", "status": "ERROR", "detail": "Сбой сети; проверка не завершена"},
        {"service": "Timeweb Cloud", "check": "Server · ROM #1", "status": "WARN", "detail": "61%"},
    ]}
    message = summarize(data)
    assert "PRIVATE-LAPTOP" not in message and "SECRET-PHONE" not in message
    assert "Сбой сети; проверка не завершена" in message
    assert "61%" in message
    assert "❌ SmartVhod" in message


def test_summary_converts_utc_checked_at_to_moscow():
    data = {"checked_at": "2026-09-28T11:00:01+00:00", "exit_code": 0, "findings": []}
    assert "28.09.2026, 14:00 МСК" in summarize(data)


def test_main_returns_failure_on_invalid_child_report(monkeypatch, capsys):
    monkeypatch.setattr(daily_check.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "invalid json", "secret"))
    assert daily_check.main() == 1
    assert "secret" not in capsys.readouterr().out


def test_main_propagates_structured_check_failure(monkeypatch, capsys):
    report = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 2, "findings": [
        {"service": "Cloud", "check": "Обход", "status": "ERROR", "detail": "Ошибка сети"},
    ]}
    monkeypatch.setattr(daily_check.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 2, json.dumps(report), ""))
    # The check ran and produced a structured ERROR: deliver its detailed digest,
    # rather than replacing it with Hermes's generic script-failed alert.
    assert daily_check.main() == 0
    assert "Ошибка сети" in capsys.readouterr().out


def test_main_retries_transient_smartvhod_error_once(monkeypatch, capsys):
    full = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 2, "findings": [
        {"service": "SmartVhod", "check": "Обход", "status": "ERROR", "detail": "Таймаут запроса (20 с)"},
        {"service": "CRM", "check": "Системные новости", "status": "OK", "detail": "Нет"},
    ]}
    retry = {"checked_at": "2026-09-28T11:00:15+00:00", "exit_code": 1, "findings": [
        {"service": "SmartVhod", "check": "Прогноз", "status": "WARN", "detail": "Пополнить 900 ₽"},
    ]}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        report = full if len(calls) == 1 else retry
        return subprocess.CompletedProcess(cmd, report["exit_code"], json.dumps(report), "")

    monkeypatch.setattr(daily_check.subprocess, "run", run)
    assert daily_check.main() == 0
    output = capsys.readouterr().out
    assert "Пополнить 900 ₽" in output and "Таймаут запроса" not in output
    assert "⚠️ SmartVhod" in output and "✅ CRM" in output
    assert calls == [daily_check.COMMAND, [*daily_check.COMMAND, "--service", "smartvhod"]]


def test_main_does_not_retry_cloud_auth_errors(monkeypatch, capsys):
    report = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 2, "findings": [
        {"service": "Timeweb Cloud", "check": "Обход", "status": "ERROR", "detail": "Cloud отклонил вход"},
    ]}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 2, json.dumps(report), "")

    monkeypatch.setattr(daily_check.subprocess, "run", run)
    assert daily_check.main() == 0
    assert "Cloud отклонил вход" in capsys.readouterr().out
    assert calls == [daily_check.COMMAND]


def test_main_retries_smartvhod_timeout_with_dependent_incomplete_forecast(monkeypatch, capsys):
    full = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 2, "findings": [
        {"service": "SmartVhod", "check": "Баланс", "status": "ERROR", "detail": "Таймаут запроса (20 с)"},
        {"service": "SmartVhod", "check": "Прогноз", "status": "ERROR", "detail": "Расчёт неполный: не все балансы, подписки или тарифы получены"},
    ]}
    retry = {"checked_at": "2026-09-28T11:01:00+00:00", "exit_code": 0, "findings": [
        {"service": "SmartVhod", "check": "Баланс", "status": "OK", "detail": "300 ₽"},
        {"service": "SmartVhod", "check": "Прогноз", "status": "OK", "detail": "Баланс покрывает продления"},
    ]}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        report = full if len(calls) == 1 else retry
        return subprocess.CompletedProcess(cmd, report["exit_code"], json.dumps(report), "")

    monkeypatch.setattr(daily_check.subprocess, "run", run)
    assert daily_check.main() == 0
    output = capsys.readouterr().out
    assert "Расчёт неполный" not in output
    assert "Восстановились после повторной попытки: SmartVhod" in output
    assert calls == [daily_check.COMMAND, [*daily_check.COMMAND, "--service", "smartvhod"]]


def test_skipped_service_is_not_marked_ok():
    report = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 0, "findings": [
        {"service": "CRM", "check": "Обход", "status": "SKIP", "detail": "Отложено"},
    ]}
    assert "⏭️ CRM" in summarize(report)


def test_incomplete_smartvhod_retry_does_not_erase_original_error(monkeypatch, capsys):
    full = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 2, "findings": [
        {"service": "SmartVhod", "check": "Баланс", "status": "ERROR", "detail": "Таймаут запроса (20 с)"},
        {"service": "SmartVhod", "check": "Прогноз", "status": "ERROR", "detail": "Расчёт неполный: не все балансы, подписки или тарифы получены"},
    ]}
    incomplete = {"checked_at": "2026-09-28T11:01:00+00:00", "exit_code": 0, "findings": [
        {"service": "SmartVhod", "check": "Баланс", "status": "OK", "detail": "300 ₽"},
    ]}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        report = full if len(calls) == 1 else incomplete
        return subprocess.CompletedProcess(cmd, report["exit_code"], json.dumps(report), "")

    monkeypatch.setattr(daily_check.subprocess, "run", run)
    assert daily_check.main() == 0
    output = capsys.readouterr().out
    assert "Проверка неполная" in output and "Расчёт неполный" in output
    assert "Восстановились" not in output
    assert len(calls) == 2


def test_main_retries_transient_crm_http_502(monkeypatch, capsys):
    failed = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 2, "findings": [
        {"service": "CRM", "check": "Обход", "status": "ERROR", "detail": "HTTP 502; проверьте доступ к кабинету"},
    ]}
    recovered = {"checked_at": "2026-09-28T11:01:00+00:00", "exit_code": 0, "findings": [
        {"service": "CRM", "check": "Системные новости", "status": "OK", "detail": "Непрочитанных объявлений нет"},
    ]}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        report = failed if len(calls) == 1 else recovered
        return subprocess.CompletedProcess(cmd, report["exit_code"], json.dumps(report), "")

    monkeypatch.setattr(daily_check.subprocess, "run", run)
    assert daily_check.main() == 0
    output = capsys.readouterr().out
    assert "✅ CRM" in output and "HTTP 502" not in output
    assert "Восстановились после повторной попытки: CRM" in output
    assert calls == [daily_check.COMMAND, [*daily_check.COMMAND, "--service", "crm"]]


def test_only_forecast_assumption_warning_is_explained():
    report = {"checked_at": "2026-09-28T11:00:00+00:00", "exit_code": 1, "findings": [
        {"service": "SmartVhod", "check": "Условия прогноза", "status": "WARN", "detail": "Сохранение всех подписок, включая ручное продление; цены на момент обхода. Тариф автосписания нужно сверить с кабинетом."},
    ]}
    text = summarize(report)
    assert "⚠️ SmartVhod" in text
    assert "тариф автосписания" in text.lower()
    assert "ручные продления" in text.lower()
    assert "цены на момент проверки" in text.lower()
