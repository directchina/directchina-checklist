"""Opt-in per-request Timeweb route outside Happ without changing global VPN rules.

The checklist is synchronous; the scoped patch is active only while a selected
Timeweb service runs. Bind BEFORE connect: binding afterward cannot alter egress.
"""

import os
import socket
from contextlib import contextmanager

TIMEWEB_HOSTS = frozenset({
    "timeweb.cloud", "hosting.timeweb.ru", "hosting-gw.timeweb.ru"
})
DEFAULT_TIMEOUT = object()


@contextmanager
def direct_route(interface):
    if not interface:
        yield
        return
    if any(os.environ.get(key) for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    )):
        raise RuntimeError("Proxy environment conflicts with direct Timeweb route")
    if not hasattr(socket, "SO_BINDTODEVICE"):
        raise RuntimeError("Прямой маршрут требует Linux SO_BINDTODEVICE")
    original = socket.create_connection

    def connect(address, timeout=DEFAULT_TIMEOUT, source_address=None, *, all_errors=False):
        host, port = address
        if host not in TIMEWEB_HOSTS:
            if timeout is DEFAULT_TIMEOUT:
                return original(address, **({"all_errors": True} if all_errors else {}))
            return original(address, timeout, source_address, all_errors=all_errors)
        errors = []
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
                if timeout is not DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                errors.append(exc)
                if sock is not None:
                    sock.close()
        if all_errors:
            raise ExceptionGroup("Timeweb direct connection failed", errors)
        raise errors[-1] if errors else OSError("No address for Timeweb host")

    socket.create_connection = connect
    try:
        yield
    finally:
        socket.create_connection = original
