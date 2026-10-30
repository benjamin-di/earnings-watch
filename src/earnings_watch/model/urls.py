"""Syntactic URL policy; DNS policy belongs to the transport."""

import ipaddress
from urllib.parse import urlsplit, urlunsplit


def public_url(value: str) -> str:
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("URL contains whitespace or control characters")
    parts = urlsplit(value)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not host
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("a public HTTPS URL without credentials is required")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith(".") or not host.isascii():
            raise ValueError("a public ASCII hostname is required") from None
    else:
        if not address.is_global:
            raise ValueError("private addresses are refused")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
