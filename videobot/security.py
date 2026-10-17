"""Limit public URL downloads to HTTP(S); guard worker socket connections too."""

import ipaddress
import re
import socket
import sys
from urllib.parse import urlsplit, urlunsplit


class UnsafeURL(ValueError):
    pass


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        # Reject transition mechanisms, which can conceal private IPv4 endpoints.
        if isinstance(address, ipaddress.IPv6Address) and (address.sixtofour or address.teredo):
            return False
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


def validate_url(value: str, *, resolve: bool = False) -> str:
    value = value.strip()
    if not value or len(value) > 4096 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise UnsafeURL("Некорректная ссылка.")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("Некорректный адрес.") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise UnsafeURL("Пришлите ссылку, начинающуюся с https:// или http://.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("Ссылки с логином или паролем в адресе не принимаются.")
    if port not in (None, 80, 443):
        raise UnsafeURL("Поддерживаются сайты на стандартных HTTP(S) портах.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if (host in ("localhost", "localhost.localdomain") or host.endswith(
                (".localhost", ".local", ".internal", ".home", ".lan", ".home.arpa"))
                or "." not in host or re.fullmatch(r"[0-9.]+", host)):
            raise UnsafeURL("Нужна ссылка на общедоступный сайт.") from None
    else:
        if not public_ip(host):
            raise UnsafeURL("Локальные и служебные адреса запрещены.")
    if resolve:
        try:
            addresses = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80),
                                           type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeURL("Не удалось найти сайт по этому адресу.") from exc
        if not addresses or any(not public_ip(item[4][0]) for item in addresses):
            raise UnsafeURL("Адрес сайта ведёт в закрытую сеть.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def install_network_guard() -> None:
    """Run only in the isolated downloader process, never in the Telegram process.

    Python-level requests, including redirects and DNS rebinding, pass the audit
    hook with the actual destination address. Native curl handlers are disabled
    by the downloader. JS is only used by yt-dlp's installed challenge solver.
    This is defense in depth, not an OS security sandbox.
    """
    def audit(event, args):
        if event != "socket.connect":
            return
        sock, address = args
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            raise OSError("Blocked non-IP connection")
        host, port = address[:2]
        if port not in (80, 443) or not public_ip(host):
            raise OSError("Blocked non-public destination")

    sys.addaudithook(audit)
