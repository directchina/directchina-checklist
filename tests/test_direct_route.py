import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from checklist.direct_route import direct_route


def test_direct_route_binds_timeweb_before_connect_and_restores(monkeypatch):
    original = socket.create_connection
    calls = []

    class FakeSocket:
        def setsockopt(self, *args):
            calls.append(("bind-device", args))

        def settimeout(self, value):
            calls.append(("timeout", value))

        def connect(self, address):
            calls.append(("connect", address))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("178.248.239.157", 443))])
    monkeypatch.setattr(socket, "socket", lambda *args: FakeSocket())
    with direct_route("enp9s0"):
        connection = socket.create_connection(("timeweb.cloud", 443), timeout=5)
        assert isinstance(connection, FakeSocket)
        assert calls[0] == ("bind-device", (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"enp9s0\0"))
        assert calls[1:] == [("timeout", 5), ("connect", ("178.248.239.157", 443))]
    assert socket.create_connection is original


def test_direct_route_leaves_other_hosts_alone(monkeypatch):
    calls = []
    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return "original"
    monkeypatch.setattr(socket, "create_connection", original)
    with direct_route("enp9s0"):
        assert socket.create_connection(("smartvhod.ru", 443), timeout=9) == "original"
    assert calls == [((("smartvhod.ru", 443), 9, None), {"all_errors": False})]


def test_direct_route_preserves_default_timeout_for_other_hosts(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: calls.append((args, kwargs)))
    with direct_route("enp9s0"):
        socket.create_connection(("smartvhod.ru", 443))
    assert calls == [((("smartvhod.ru", 443),), {})]


def test_direct_route_never_falls_back_to_vpn_on_binding_error(monkeypatch):
    class FakeSocket:
        def setsockopt(self, *_):
            raise OSError("no interface")
        def close(self):
            pass
    monkeypatch.setattr(socket, "socket", lambda *args: FakeSocket())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("178.248.239.157", 443))])
    with direct_route("enp9s0"), pytest.raises(OSError, match="no interface"):
        socket.create_connection(("timeweb.cloud", 443), timeout=5)


def test_cli_routes_timeweb_only_when_direct_interface_selected(monkeypatch, capsys):
    from checklist import __main__ as app

    original = socket.create_connection
    observed = []

    def record(report, config, now):
        observed.append(socket.create_connection is not original)
        report.add("test", "network", "checked")

    monkeypatch.setattr(app, "SERVICES", {"hosting": ("Hosting", record), "crm": ("CRM", record)})
    assert app.main(["--service", "all", "--direct-interface", "enp9s0", "--json"]) == 0
    assert observed == [True, False]
    assert socket.create_connection is original
    assert capsys.readouterr().out


@pytest.mark.parametrize("name", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"])
def test_direct_route_rejects_proxy_environment(monkeypatch, name):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(name, "http://127.0.0.1:8080")
    with pytest.raises(RuntimeError, match="[Pp]roxy"), direct_route("enp9s0"):
        pass


def test_cli_reports_direct_route_setup_failure_as_structured_error(monkeypatch, capsys):
    import json

    from checklist import __main__ as app

    def failed_route(_):
        raise RuntimeError("route failure")

    monkeypatch.setattr(app, "direct_route", failed_route)
    monkeypatch.setattr(app, "SERVICES", {"hosting": ("Hosting", lambda *args: None)})
    assert app.main(["--service", "hosting", "--direct-interface", "enp9s0", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["findings"][0]["status"] == "ERROR"


def test_real_httpx_clients_from_timeweb_adapters_use_device_binding(monkeypatch):
    from checklist.session import Cabinet
    from checklist.timeweb_cloud import Cloud

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_socket = socket.socket
    real_getaddrinfo = socket.getaddrinfo
    bindings = []

    class RecordingSocket(real_socket):
        def setsockopt(self, level, option, value):
            if option == socket.SO_BINDTODEVICE:
                bindings.append(value)
                return
            return super().setsockopt(level, option, value)

    def local_dns(host, port, *args, **kwargs):
        if host in {"timeweb.cloud", "hosting.timeweb.ru"}:
            return real_getaddrinfo("127.0.0.1", server.server_port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", RecordingSocket)
    monkeypatch.setattr(socket, "getaddrinfo", local_dns)
    try:
        with direct_route("enp9s0"), Cloud() as cloud, Cabinet("https://hosting.timeweb.ru", []) as hosting:
            # Use the adapters' actual httpx clients; only the local HTTP test URL
            # substitutes for TLS/auth. No external DNS or account is contacted.
            assert cloud.client.get(f"http://timeweb.cloud:{server.server_port}/").text == "ok"
            assert hosting.client.get(f"http://hosting.timeweb.ru:{server.server_port}/").text == "ok"
        assert bindings == [b"enp9s0\0", b"enp9s0\0"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
