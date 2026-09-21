from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from checklist import cloud_servers as servers
from checklist.core import CheckError, Report
from checklist.timeweb_cloud import Cloud

NOW = datetime(2026, 9, 21, 13, tzinfo=UTC)


def server(sid=1):
    return {
        "id": sid,
        "name": f"Server {sid}",
        "status": "on",
        "is_blocked": False,
        "disks": [{"id": 100 + sid, "size": 100, "used": 50}],
    }


def stats(value=50, stamp=NOW):
    return {
        servers.cpu_path(1, NOW): {
            "statistics": [
                {
                    "name": "system.cpu.util",
                    "list": [{"time": stamp.isoformat(), "value": value}],
                }
            ]
        },
        "/api/v3/servers/1/statistics": {
            "ram": {
                "is_actual": True,
                "statistic": [
                    {
                        "logged_at": stamp.isoformat(),
                        "total": 200,
                        "used": None if value is None else value * 2,
                        "used_cached": 199,
                    }
                ],
            }
        },
    }


def check(data=None, entry=None):
    data = stats() if data is None else data
    entry = server() if entry is None else entry
    calls = []

    def serve(request):
        path = request.url.raw_path.decode().split("?")[0]
        calls.append(request)
        return httpx.Response(200, json=data[path])

    with Cloud(httpx.MockTransport(serve)) as client:
        report = Report()
        servers.check_server(report, client, entry, NOW)
    assert all(r.method == "GET" and r.url.host == "timeweb.cloud" for r in calls)
    return report


@pytest.mark.parametrize(
    "value, expected",
    [(0, "OK"), (49.99, "OK"), (50, "OK"), (50.001, "WARN"), (100, "WARN")],
)
def test_strict_threshold_for_all_three_resources(value, expected):
    entry = server()
    entry["disks"][0]["used"] = value
    report = check(stats(value), entry)
    assert len(report.findings) == 4
    assert [f.status for f in report.findings] == ["OK", expected, expected, expected]


def test_latest_timestamp_not_api_order_or_peak():
    data = stats(20)
    data[servers.cpu_path(1, NOW)]["statistics"][0]["list"].append(
        {"time": (NOW - timedelta(minutes=5)).isoformat(), "value": 99}
    )
    data["/api/v3/servers/1/statistics"]["ram"]["statistic"].insert(
        0,
        {
            "logged_at": (NOW - timedelta(minutes=5)).isoformat(),
            "total": 200,
            "used": 198,
        },
    )
    report = check(data)
    assert report.exit_code == 0
    assert "20.00%" in report.findings[1].detail
    assert "20.00%" in report.findings[2].detail


@pytest.mark.parametrize("kind", ["empty", "null", "stale", "not_actual"])
def test_missing_and_stale_data_warn(kind):
    data = stats(
        None if kind == "null" else 20,
        NOW - timedelta(minutes=31) if kind == "stale" else NOW,
    )
    ram = data["/api/v3/servers/1/statistics"]["ram"]
    if kind == "empty":
        data[servers.cpu_path(1, NOW)]["statistics"] = []
        ram["statistic"] = []
    if kind == "not_actual":
        ram["is_actual"] = False
    report = check(data)
    assert report.findings[2].status == "WARN"
    if kind != "not_actual":
        assert report.findings[1].status == "WARN"


@pytest.mark.parametrize("value", [-1, 101, "NaN", "Infinity"])
def test_invalid_cpu_does_not_hide_other_metrics(value):
    data = stats()
    data[servers.cpu_path(1, NOW)]["statistics"][0]["list"][0]["value"] = value
    report = check(data)
    assert report.findings[1].status == "ERROR"
    assert report.findings[2].status == report.findings[3].status == "OK"


def test_newest_missing_value_does_not_fall_back_to_healthy_old_value():
    data = stats(None)
    data[servers.cpu_path(1, NOW)]["statistics"][0]["list"].append(
        {"time": (NOW - timedelta(minutes=2)).isoformat(), "value": 10}
    )
    assert check(data).findings[1].status == "WARN"


@pytest.mark.parametrize(
    "state, blocked", [("off", False), ("on", True), ("installing", False)]
)
def test_non_running_or_blocked_server_warns(state, blocked):
    entry = server()
    entry.update(status=state, is_blocked=blocked)
    assert check(entry=entry).findings[0].status == "WARN"


def test_each_disk_checked_individually():
    entry = server()
    entry["disks"] += [
        {"id": 102, "size": 10, "used": 9},
        {"id": 103, "size": 10, "used": None},
    ]
    report = check(entry=entry)
    assert [(f.check.split(" · ")[-1], f.status) for f in report.findings[3:]] == [
        ("ROM #101", "OK"),
        ("ROM #102", "WARN"),
        ("ROM #103", "WARN"),
    ]


def test_bad_disk_does_not_prevent_next_disk_check():
    entry = server()
    entry["disks"][0]["size"] = 0
    entry["disks"].append({"id": 102, "size": 10, "used": 9})
    report = check(entry=entry)
    assert report.findings[-2].status == "ERROR"
    assert report.findings[-1].status == "WARN"


@pytest.mark.parametrize(
    "stamp", [NOW + timedelta(minutes=6), NOW.replace(tzinfo=None)]
)
def test_invalid_timestamps_fail(stamp):
    report = check(stats(10, stamp))
    assert report.findings[1].status == report.findings[2].status == "ERROR"


def test_list_pagination_and_failure_isolation():
    pages = [server(1), server(2)]
    data = stats()
    data[servers.cpu_path(2, NOW)] = deepcopy(data[servers.cpu_path(1, NOW)])
    data["/api/v3/servers/2/statistics"] = deepcopy(
        data["/api/v3/servers/1/statistics"]
    )
    offsets = []

    def serve(request):
        path = request.url.raw_path.decode().split("?")[0]
        if path == servers.SERVERS:
            offset = int(request.url.params["offset"])
            offsets.append(offset)
            return httpx.Response(
                200, json={"servers": pages[offset : offset + 1], "meta": {"total": 2}}
            )
        if path == servers.cpu_path(1, NOW):
            return httpx.Response(503, text="private-server-password")
        return httpx.Response(200, json=data[path])

    with Cloud(httpx.MockTransport(serve)) as client:
        report = Report()
        servers.run(report, client, NOW)
    assert offsets == [0, 1]
    assert len(report.findings) == 8
    assert report.findings[1].status == "ERROR"
    assert all(f.status == "OK" for f in report.findings[4:])
    assert all("private-server-password" not in f.detail for f in report.findings)


def test_partial_list_is_not_reported_as_complete():
    with (
        Cloud(
            httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"servers": [], "meta": {"total": 2}}
                )
            )
        ) as client,
        pytest.raises(CheckError, match="неполный"),
    ):
        servers.list_servers(client)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/servers/1/reboot",
        "/api/v3/servers/1/statistics/../../reboot",
        "/api/v1/servers/1/statistics/2026-09-21T13%3A00%3A00Z/1/system.cpu.util/../../reboot",
        "https://evil.test/api/v3/servers/1/statistics",
        "/api/v3/servers/1/statistics?redirect=evil",
    ],
)
def test_new_read_allowlist_rejects_other_routes(path):
    with (
        Cloud(
            httpx.MockTransport(lambda r: pytest.fail("Unexpected request"))
        ) as client,
        pytest.raises(CheckError),
    ):
        client.request("GET", path)


def test_statistics_cannot_be_posted():
    with Cloud(
        httpx.MockTransport(lambda r: pytest.fail("Unexpected request"))
    ) as client:
        for path in (
            servers.SERVERS,
            servers.cpu_path(1, NOW),
            "/api/v3/servers/1/statistics",
        ):
            with pytest.raises(CheckError):
                client.request("POST", path)


def test_percentage_is_decimal_and_not_rounded_before_comparison():
    assert servers.percent("50.0001") > Decimal(50)
