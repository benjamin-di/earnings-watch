"""Syntactic URL policy; DNS policy belongs to the transport."""

import ipaddress
from urllib.parse import urlsplit, urlunsplit

_TWO_PART_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "co.nz",
        "org.nz",
        "com.au",
        "net.au",
        "org.au",
        "com.br",
        "net.br",
        "org.br",
        "co.jp",
        "ne.jp",
        "or.jp",
        "co.kr",
        "or.kr",
        "com.cn",
        "net.cn",
        "org.cn",
        "com.sg",
        "org.sg",
        "com.tw",
        "org.tw",
        "com.my",
        "co.za",
        "co.in",
        "co.il",
        "com.mx",
        "com.id",
        "co.id",
        "com.tr",
        "com.ar",
        "com.pe",
        "com.co",
        "com.ph",
        "com.vn",
        "co.th",
        "com.eg",
        "com.sa",
        "com.ua",
        "com.pl",
        "com.es",
        "com.pt",
        "com.gr",
        "com.cy",
        "com.kz",
    }
)

_SHARED_PLATFORM_SUFFIXES = frozenset(
    {
        "q4cdn.com",
        "cloudfront.net",
        "github.io",
        "amazonaws.com",
        "azurewebsites.net",
        "hkexnews.hk",
        "sec.gov",
        "asx.com.au",
        "netlify.app",
        "vercel.app",
        "pages.dev",
        "googleapis.com",
        "herokuapp.com",
        "firebaseapp.com",
        "sharepoint.com",
        "force.com",
    }
)


def apex_domain(host: str) -> str:
    """Return the registrable apex domain for a hostname, or '' for IPs, bare suffixes, or shared platforms."""
    cleaned = host.strip().rstrip(".").lower()
    if not cleaned:
        return ""
    try:
        ipaddress.ip_address(cleaned.strip("[]"))
    except ValueError:
        pass
    else:
        return ""
    parts = cleaned.split(".")
    if len(parts) < 2 or any(not part for part in parts):
        return ""
    last_two = ".".join(parts[-2:])
    if last_two in _TWO_PART_SUFFIXES:
        if len(parts) < 3:
            return ""
        candidate = ".".join(parts[-3:])
    else:
        candidate = last_two
    if candidate in _SHARED_PLATFORM_SUFFIXES or last_two in _SHARED_PLATFORM_SUFFIXES:
        return ""
    return candidate


def same_apex(host_a: str, host_b: str) -> bool:
    """Whether two hostnames share the same registrable apex domain."""
    a, b = apex_domain(host_a), apex_domain(host_b)
    return bool(a and b and a == b)


def public_url(value: str) -> str:
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("URL contains whitespace or control characters")
    parts = urlsplit(value)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
    ):
        raise ValueError("a public HTTPS URL without credentials is required")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith(".") or not host.isascii():
            raise ValueError("a public ASCII hostname is required") from None
    else:
        if not address.is_global or address.is_multicast:
            raise ValueError("private addresses are refused")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
