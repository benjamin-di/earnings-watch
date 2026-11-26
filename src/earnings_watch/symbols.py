"""Pure symbol and exchange normalization for tickers, ISINs, and IR URL hints."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .errors import EarningsError
from .model.urls import public_url

TICKER_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9.\-]{0,18}[A-Z0-9])?$")
EXCHANGE_RE = re.compile(r"^[A-Z0-9]{4}$")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# Common brokerage and Yahoo Finance exchange suffixes mapped to ISO 10383 MICs.
# Share-class suffixes (.A, .B, -A, -B) are intentionally excluded so tickers like
# BRK.B and BF-B remain intact.
SUFFIX_TO_MIC: dict[str, str] = {
    "AS": "XAMS",
    "AX": "XASX",
    "BR": "XBRU",
    "CO": "XCSE",
    "DE": "XETR",
    "HE": "XHEL",
    "HK": "XHKG",
    "KS": "XKRX",
    "L": "XLON",
    "MC": "XMAD",
    "MI": "XMIL",
    "OL": "XOSL",
    "PA": "XPAR",
    "SI": "XSES",
    "SS": "XSHG",
    "ST": "XSTO",
    "SW": "XSWX",
    "SZ": "XSHE",
    "T": "XTKS",
    "TO": "XTSE",
    "TW": "XTAI",
    "VI": "XWBO",
}

EXCHANGE_ALIASES: dict[str, str] = {
    "AMEX": "XASE",
    "AMS": "XAMS",
    "ARCA": "ARCX",
    "ASX": "XASX",
    "EPA": "XPAR",
    "ETR": "XETR",
    "FRA": "XETR",
    "HKEX": "XHKG",
    "HKG": "XHKG",
    "JPX": "XTKS",
    "KRX": "XKRX",
    "LON": "XLON",
    "LSE": "XLON",
    "NAS": "XNAS",
    "NASDAQ": "XNAS",
    "NSDQ": "XNAS",
    "NYSE": "XNYS",
    "PAR": "XPAR",
    "SEHK": "XHKG",
    "SGX": "XSES",
    "SIX": "XSWX",
    "SSE": "XSHG",
    "SWX": "XSWX",
    "SZSE": "XSHE",
    "TSE": "XTKS",
    "TSX": "XTSE",
    "TWSE": "XTAI",
    "TYO": "XTKS",
    "XETRA": "XETR",
}


def normalize_exchange(raw: str) -> str:
    cleaned = raw.strip().upper()
    return EXCHANGE_ALIASES.get(cleaned, cleaned)


def is_valid_ticker(value: str) -> bool:
    return bool(TICKER_RE.fullmatch(value))


def is_valid_exchange(value: str) -> bool:
    return bool(EXCHANGE_RE.fullmatch(value))


def is_valid_isin(value: str) -> bool:
    return bool(ISIN_RE.fullmatch(value))


def parse_symbol(raw_value: str, raw_exchange: str = "") -> tuple[str, str]:
    """Normalize a user-supplied ticker/ISIN and optional exchange into (symbol, MIC)."""
    value = raw_value.strip().upper()
    exchange = normalize_exchange(raw_exchange)
    if ":" in value:
        prefix, symbol = value.split(":", 1)
        prefix, symbol = normalize_exchange(prefix), symbol.strip()
        if exchange and prefix and exchange != prefix:
            raise EarningsError("invalid_argument", "Conflicting exchange values.")
        exchange = exchange or prefix
        value = symbol
    if "." in value:
        base, suffix = value.rsplit(".", 1)
        mic = SUFFIX_TO_MIC.get(suffix)
        if mic and base:
            if exchange and exchange != mic:
                raise EarningsError("invalid_argument", "Conflicting exchange values.")
            value = base
            exchange = mic
    return value, exchange


def validate_ir_url(raw: str) -> str:
    """Validate and normalize an optional user-supplied investor-relations URL hint."""
    url = raw.strip()
    if not url or len(url) > 500 or any(ch.isspace() for ch in url):
        raise EarningsError("invalid_argument", "IR URL must be a public https:// URL.")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise EarningsError("invalid_argument", "IR URL must be a public https:// URL.")
    try:
        return public_url(url)
    except ValueError as exc:
        raise EarningsError("invalid_argument", "IR URL must be a public https:// URL.") from exc
