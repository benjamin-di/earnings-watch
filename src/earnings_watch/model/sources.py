from __future__ import annotations
import re
from dataclasses import dataclass
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from earnings_watch.model.events import EventType


class Membership(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"


class FetchStrategy(StrEnum):
    PLAIN = "plain"
    WEBSEARCH = "websearch"


class PeriodScheme(StrEnum):
    CALENDAR_Q = "calendar-q"
    FISCAL_Q = "fiscal-q"
    HALF_YEAR = "half-year"


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    issuer: str
    ticker: str
    name: str
    exchange: str | None = Field(default=None, pattern=r"^[A-Z0-9]{4}$")
    isin: str | None = Field(default=None, pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
    notes: str = ""
    extensions: dict = Field(default_factory=dict)
    calendar_url: str
    fetch: FetchStrategy
    period_scheme: PeriodScheme
    fy_end_month: int = Field(ge=1, le=12)
    results_url: str | None = None
    announcements_url: str | None = None
    presentations_url: str | None = None
    transcripts_url: str | None = None
    archive_url: str | None = None
    render_mode: str | None = None
    ir_platform: str | None = None
    timing_url_prefixes: list[str] = Field(default_factory=list)
    calendar_timezone: str | None = None
    calendar_timezone_basis: str | None = None
    sec_cik: int | None = Field(default=None, ge=1, le=9999999999)
    hkex_stock_id: str | None = Field(default=None, pattern=r"^[0-9]{1,12}$")
    lei: str | None = Field(default=None, pattern=r"^[A-Z0-9]{18}[0-9]{2}$")

    @model_validator(mode="after")
    def _timing_configuration(self):
        from earnings_watch.model.urls import public_url

        for url in [
            self.calendar_url,
            self.results_url,
            self.announcements_url,
            self.presentations_url,
            self.transcripts_url,
            self.archive_url,
            *self.timing_url_prefixes,
        ]:
            if url:
                public_url(url)
        if self.render_mode not in {None, "http", "browser"}:
            raise ValueError("render_mode must be 'http', 'browser', or None")
        if self.ir_platform not in {None, "q4", "generic"}:
            raise ValueError("ir_platform must be 'q4', 'generic', or None")
        if bool(self.calendar_timezone) != bool(self.calendar_timezone_basis):
            raise ValueError("calendar timezone requires a documented basis")
        if self.calendar_timezone:
            try:
                ZoneInfo(self.calendar_timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("unknown calendar timezone") from exc
        return self

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch("[a-z0-9][a-z0-9-]*", value):
            raise ValueError("key must be a lowercase path-safe slug")
        return value

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Z0-9](?:[A-Z0-9.\-]{0,18}[A-Z0-9])?", value):
            raise ValueError("ticker must contain only uppercase letters, digits, dots, or hyphens")
        return value

    @field_validator("issuer", "name", "calendar_url")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must be non-empty")
        return value


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[Source]
    ignore: list[str] = Field(default_factory=list)

    @field_validator("ignore")
    @classmethod
    def _ignore(cls, value: list[str]) -> list[str]:
        cleaned = [entry.strip() for entry in value]
        if not all(cleaned):
            raise ValueError("ignore entries must be non-empty issuer names")
        duplicates = sorted({entry for entry in cleaned if cleaned.count(entry) > 1})
        if duplicates:
            raise ValueError(f"duplicate ignore entries: {', '.join(duplicates)}")
        return cleaned


_YEAR = "(?P<year>(?:20\\d{2}|\\d{2}))"
_QUARTER_PATTERNS = (
    re.compile(f"^{_YEAR}\\s*Q(?P<quarter>[1-4])$", re.IGNORECASE),
    re.compile(f"^Q(?P<quarter>[1-4])\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^(?P<quarter>[1-4])Q\\s*{_YEAR}$", re.IGNORECASE),
)
_DESCRIPTIVE_QUARTER_PATTERNS = (
    re.compile(f"^(?P<quarter>[1-4])(?:ST|ND|RD|TH)\\s+QUARTER\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^{_YEAR}\\s*(?P<quarter>[1-4])(?:ST|ND|RD|TH)\\s+QUARTER$", re.IGNORECASE),
    re.compile(
        f"^(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\\s+QUARTER\\s*{_YEAR}$", re.IGNORECASE
    ),
    re.compile(
        f"^{_YEAR}\\s*(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\\s+QUARTER$", re.IGNORECASE
    ),
    re.compile(
        f"^(?P<quarter>[1-4])(?:ST|ND|RD|TH)\\s+QUARTER\\s+AND\\s+(?:FIRST\\s+HALF|SECOND\\s+HALF|NINE[-\\s]?MONTHS?|FULL[-\\s]?YEAR)\\s*{_YEAR}$",
        re.IGNORECASE,
    ),
    re.compile(
        f"^(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\\s+QUARTER\\s+AND\\s+(?:FIRST\\s+HALF|SECOND\\s+HALF|NINE[-\\s]?MONTHS?|FULL[-\\s]?YEAR)\\s*{_YEAR}$",
        re.IGNORECASE,
    ),
)
_FISCAL_PATTERNS = (
    re.compile(
        f"^FISCAL\\s+(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\\s+QUARTER\\s*{_YEAR}$",
        re.IGNORECASE,
    ),
    re.compile(
        f"^(?P<quarter_word>FIRST|SECOND|THIRD|FOURTH)\\s+QUARTER\\s+(?:OF\\s+)?FISCAL(?:\\s+YEAR)?\\s*{_YEAR}$",
        re.IGNORECASE,
    ),
    re.compile(
        f"^(?P<quarter>[1-4])(?:ST|ND|RD|TH)\\s+QUARTER\\s+(?:OF\\s+)?FISCAL(?:\\s+YEAR)?\\s*{_YEAR}$",
        re.IGNORECASE,
    ),
    re.compile(f"^{_YEAR}\\s*FQ(?P<quarter>[1-4])$", re.IGNORECASE),
    re.compile(f"^FQ(?P<quarter>[1-4])\\s*(?:FY)?\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^Q(?P<quarter>[1-4])\\s*FY\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^FY\\s*{_YEAR}\\s*Q(?P<quarter>[1-4])$", re.IGNORECASE),
)
_HALF_PATTERNS = (
    re.compile(f"^{_YEAR}\\s*H(?P<half>[12])$", re.IGNORECASE),
    re.compile(f"^H(?P<half>[12])\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^(?P<half>[12])H\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^(?P<half_word>FIRST|SECOND)\\s+HALF\\s+(?:OF\\s+)?{_YEAR}$", re.IGNORECASE),
    re.compile(f"^{_YEAR}\\s+(?P<half_word>FIRST|SECOND)\\s+HALF$", re.IGNORECASE),
    re.compile(f"^{_YEAR}\\s+HALF[-\\s]?YEAR(?:LY)?(?:\\s+(?:REPORT|RESULTS?))?$", re.IGNORECASE),
    re.compile(f"^HALF[-\\s]?YEAR(?:LY)?(?:\\s+(?:REPORT|RESULTS?))?\\s+{_YEAR}$", re.IGNORECASE),
)
_NINE_MONTH_PATTERNS = (
    re.compile(f"^(?:FIRST\\s+)?(?:9|NINE)\\s*M(?:ONTHS?)?\\s+(?:OF\\s+)?{_YEAR}$", re.IGNORECASE),
    re.compile(f"^(?:9M|9\\s+MONTHS?)\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^{_YEAR}\\s*(?:9M|9\\s+MONTHS?)$", re.IGNORECASE),
)
_FULL_YEAR_PATTERNS = (
    re.compile(f"^(?:FY|FULL[-\\s]+YEAR)\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^{_YEAR}\\s*(?:FY|FULL[-\\s]+YEAR)$", re.IGNORECASE),
    re.compile(f"^ANNUAL\\s+RESULTS?\\s*{_YEAR}$", re.IGNORECASE),
    re.compile(f"^{_YEAR}\\s*ANNUAL\\s+RESULTS?$", re.IGNORECASE),
    re.compile(f"^ANNUAL\\s+{_YEAR}$", re.IGNORECASE),
    re.compile(f"^{_YEAR}\\s+ANNUAL$", re.IGNORECASE),
    re.compile(f"^(?:FY\\s*)?{_YEAR}\\s+ANNUAL\\s+(?:REPORT|RESULTS?)$", re.IGNORECASE),
    re.compile(f"^ANNUAL\\s+(?:REPORT|RESULTS?)\\s+(?:FY\\s*)?{_YEAR}$", re.IGNORECASE),
    re.compile(f"^{_YEAR}$", re.IGNORECASE),
)
_FOUR_DIGIT_YEAR = re.compile("(?<!\\d)(?P<year>20\\d{2})(?!\\d)")
_SHORT_FISCAL_YEAR = re.compile("\\bFY[\\s-]*(?P<year>\\d{2})(?!\\d)", re.IGNORECASE)


def _find_period(raw: str, patterns: tuple[re.Pattern[str], ...]) -> re.Match[str] | None:
    value = re.sub("\\s+", " ", raw.strip())
    for pattern in patterns:
        match = pattern.fullmatch(value)
        if match:
            return match
    return None


def _four_digit_year(match: re.Match[str]) -> str:
    year = match.group("year")
    return f"20{year}" if len(year) == 2 else year


def _quarter_number(match: re.Match[str]) -> int:
    quarter = match.groupdict().get("quarter")
    if quarter is not None:
        return int(quarter)
    return {"first": 1, "second": 2, "third": 3, "fourth": 4}[match.group("quarter_word").lower()]


def _non_earnings_year(raw: str, event_date) -> int:
    match = _FOUR_DIGIT_YEAR.search(raw)
    if match:
        return int(match.group("year"))
    match = _SHORT_FISCAL_YEAR.search(raw)
    if match:
        return int(f"20{match.group('year')}")
    return event_date.year


def _canonical_non_earnings_period(raw: str, event_type: EventType, event_date) -> str:
    value = re.sub("[^a-z0-9]+", " ", raw.lower()).strip()
    year = _non_earnings_year(raw, event_date)
    if event_type == EventType.AGM:
        agm_year = event_date.year if event_date is not None else year
        if re.search("\\b(?:egm|extraordinary)\\b", value):
            return f"{agm_year}-egm"
        return str(agm_year)
    if event_type == EventType.CMD:
        return str(year)
    if event_type == EventType.CONFERENCE:
        return f"{event_date.isoformat()}-conference"
    if event_type == EventType.TRADING_UPDATE:
        tokens = _period_tokens(raw)
        if len(tokens.quarters) == 1:
            quarter = next(iter(tokens.quarters))
            return f"{year}Q{quarter}-trading-update"
        if tokens.nine:
            return f"{year}Q3-trading-update"
        if tokens.first_half and not (tokens.second_half or tokens.full_year):
            return f"{year}H1-trading-update"
        if tokens.second_half and not tokens.first_half:
            return f"{year}H2-trading-update"
        return f"{event_date.isoformat()}-trading-update"
    if event_type == EventType.DIVIDEND:
        kind: str | None = None
        if "interim" in value:
            kind = "interim"
        elif re.search("\\b(?:special|extraordinary)\\b", value):
            kind = "special"
        milestone: str | None = None
        if re.search("\\bex[\\s-]*div(?:idend)?\\b|\\bex\\s+date\\b", value):
            milestone = "ex"
        elif re.search("\\brecord(?:\\s+date)?\\b", value):
            milestone = "record"
        elif re.search("\\b(?:pay|paid|payable|payment)(?:\\s+date)?\\b", value):
            milestone = "pay"
        tokens = _period_tokens(raw)
        if len(tokens.quarters) == 1:
            quarter = next(iter(tokens.quarters))
            qualifier = milestone or kind
            return (
                f"{year}Q{quarter}-{qualifier}-dividend"
                if qualifier
                else f"{year}Q{quarter}-dividend"
            )
        ym_match = re.search("\\b(20\\d{2})-(0[1-9]|1[0-2])\\b", raw)
        month_match = re.search(f"\\b(?P<month>{_MONTH_NAMES})\\b", raw, re.IGNORECASE)
        month: int | None = None
        month_year = year
        if ym_match:
            month_year = int(ym_match.group(1))
            month = int(ym_match.group(2))
        elif month_match:
            month = _MONTH_NUMBERS[month_match.group("month")[:3].lower()]
        elif "quarterly" in value:
            month_year = event_date.year
            month = event_date.month
        if month is not None:
            qualifier = milestone or kind
            return (
                f"{month_year}-{month:02d}-{qualifier}-dividend"
                if qualifier
                else f"{month_year}-{month:02d}-dividend"
            )
        parts = [str(year)]
        if kind:
            parts.append(kind)
        if milestone:
            parts.append(milestone)
        parts.append("dividend")
        return "-".join(parts)
    raise ValueError(f"unsupported event type: {event_type}")


_AS_OF_US_RE = re.compile(
    "\\bAS\\s+OF\\s+(?P<month>MARCH|JUNE|SEPTEMBER|DECEMBER)\\s+(?P<day>\\d{1,2}),?\\s+(?P<year>20\\d{2})\\b",
    re.IGNORECASE,
)
_AS_AT_RE = re.compile(
    "\\bAS\\s+AT\\s+(?P<day>\\d{1,2})\\s+(?P<month>MARCH|JUNE|SEPTEMBER|DECEMBER)\\s+(?P<year>20\\d{2})\\b",
    re.IGNORECASE,
)
_INTERIM_QUALIFIER = "(?:\\s+(?:FINANCIAL\\s+)?(?:RESULTS?|REPORT|ANNOUNCEMENT))?"
_INTERIM_PATTERNS = (
    re.compile(f"^{_YEAR}\\s+INTERIM{_INTERIM_QUALIFIER}$", re.IGNORECASE),
    re.compile(f"^INTERIM{_INTERIM_QUALIFIER}(?:\\s+FOR)?\\s+{_YEAR}$", re.IGNORECASE),
)
_MONTH_NAMES = "JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?|AUG(?:UST)?|SEPT?(?:EMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?"
_PERIOD_END_RE = re.compile(
    f"\\b(?:(?P<months>THREE|SIX|NINE|TWELVE|3|6|9|12)\\s*MONTHS?|(?P<span>(?:FINANCIAL\\s+|FISCAL\\s+)?YEAR|HALF[-\\s]?YEAR))\\s+(?:ENDED|ENDING|TO)\\s+(?:\\d{{1,2}}(?:ST|ND|RD|TH)?\\s+(?P<month_dmy>{_MONTH_NAMES})|(?P<month_mdy>{_MONTH_NAMES})\\s+\\d{{1,2}}(?:ST|ND|RD|TH)?),?\\s+(?P<year>20\\d{{2}})\\b",
    re.IGNORECASE,
)
_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_SPAN_WORDS = {"three": 3, "six": 6, "nine": 9, "twelve": 12}
_PERIOD_NOISE_TOKENS = {
    "earnings",
    "results",
    "for",
    "the",
    "of",
    "on",
    "press",
    "conference",
    "call",
    "webcast",
}
_DESCRIPTION_BREAK = re.compile("\\s+-\\s+|(?<=[0-9A-Za-z)])-\\s+")


def _strip_period_noise(raw: str) -> str:
    value = _DESCRIPTION_BREAK.split(re.sub("\\s+", " ", raw.strip()))[0]
    tokens = value.replace("(", " ").replace(")", " ").split()
    while tokens and tokens[0].lower() in _PERIOD_NOISE_TOKENS:
        tokens.pop(0)
    while tokens and tokens[-1].lower() in _PERIOD_NOISE_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def _period_end_period(raw: str, source: Source) -> str | None:
    if source.period_scheme == PeriodScheme.FISCAL_Q:
        return None
    match = _PERIOD_END_RE.search(raw)
    if match is None:
        return None
    month_name = match.group("month_dmy") or match.group("month_mdy")
    month = _MONTH_NUMBERS[month_name[:3].lower()]
    year = int(match.group("year"))
    elapsed = (month - source.fy_end_month - 1) % 12 + 1
    months = match.group("months")
    if months is not None:
        span = _SPAN_WORDS.get(months.lower()) or int(months)
    elif "half" in match.group("span").lower():
        span = 6
    else:
        span = 12
    half_year = source.period_scheme == PeriodScheme.HALF_YEAR
    suffix: str | None
    if span == 3:
        if elapsed % 3:
            return None
        suffix = {6: "H1", 12: "H2"}.get(elapsed) if half_year else f"Q{elapsed // 3}"
    elif span == 6:
        suffix = {6: "H1", 12: "H2"}.get(elapsed)
    elif span == 9:
        suffix = "Q3" if elapsed == 9 and (not half_year) else None
    else:
        suffix = ("H2" if half_year else "Q4") if elapsed == 12 else None
    if suffix is None:
        return None
    fiscal_year = year if month <= source.fy_end_month else year + 1
    return f"{fiscal_year}{suffix}"


def _match_earnings_period(raw: str, source: Source) -> str | None:
    combined = re.fullmatch(
        "\\s*([234])Q\\s*(20\\d{2}|\\d{2})\\s*(?:&|and)\\s*(1H|9M|FY)\\s*(20\\d{2}|\\d{2})\\s*",
        raw,
        re.I,
    )
    if combined:
        quarter, year, cumulative, cumulative_year = combined.groups()
        if (
            year[-2:] != cumulative_year[-2:]
            or {"2": "1H", "3": "9M", "4": "FY"}[quarter] != cumulative.upper()
        ):
            raise ValueError(f"inconsistent combined period: {raw!r}")
        raw = f"{quarter}Q{year}"
    match = _find_period(raw, _FISCAL_PATTERNS)
    if match:
        return f"{_four_digit_year(match)}FQ{_quarter_number(match)}"
    match = _find_period(raw, _HALF_PATTERNS)
    if match:
        groups = match.groupdict()
        half = groups.get("half")
        if not half:
            half_word = groups.get("half_word")
            half = {"first": "1", "second": "2"}[half_word.lower()] if half_word else "1"
        return f"{_four_digit_year(match)}H{half}"
    if source.period_scheme == PeriodScheme.HALF_YEAR:
        match = _find_period(raw, _INTERIM_PATTERNS)
        if match:
            return f"{_four_digit_year(match)}H1"
    match = _find_period(raw, _NINE_MONTH_PATTERNS)
    if match:
        suffix = "FQ3" if source.period_scheme == PeriodScheme.FISCAL_Q else "Q3"
        return f"{_four_digit_year(match)}{suffix}"
    match = _find_period(raw, _FULL_YEAR_PATTERNS)
    if match:
        suffix = {
            PeriodScheme.CALENDAR_Q: "Q4",
            PeriodScheme.FISCAL_Q: "FQ4",
            PeriodScheme.HALF_YEAR: "H2",
        }[source.period_scheme]
        return f"{_four_digit_year(match)}{suffix}"
    match = _find_period(raw, _QUARTER_PATTERNS + _DESCRIPTIVE_QUARTER_PATTERNS)
    if match:
        year = _four_digit_year(match)
        quarter = _quarter_number(match)
        if source.period_scheme == PeriodScheme.FISCAL_Q:
            return f"{year}FQ{quarter}"
        if source.period_scheme == PeriodScheme.HALF_YEAR:
            half = {2: 1, 4: 2}.get(quarter)
            if half is None:
                raise ValueError(f"quarter does not identify a half-year period: {raw!r}")
            return f"{year}H{half}"
        return f"{year}Q{quarter}"
    match = _AS_OF_US_RE.search(raw)
    if match:
        month = {"march": 3, "june": 6, "september": 9, "december": 12}[
            match.group("month").lower()
        ]
        if int(match.group("day")) != {3: 31, 6: 30, 9: 30, 12: 31}[month]:
            return None
        elapsed = (month - source.fy_end_month - 1) % 12 + 1
        if elapsed % 3:
            return None
        year = int(match.group("year")) + (month > source.fy_end_month)
        if source.period_scheme == PeriodScheme.HALF_YEAR:
            return f"{year}H{elapsed // 6}" if elapsed in {6, 12} else None
        return f"{year}{('FQ' if source.period_scheme == PeriodScheme.FISCAL_Q else 'Q')}{elapsed // 3}"
    match = _AS_AT_RE.search(raw)
    if match:
        month = {"march": 3, "june": 6, "september": 9, "december": 12}[
            match.group("month").lower()
        ]
        if int(match.group("day")) != {3: 31, 6: 30, 9: 30, 12: 31}[month]:
            return None
        elapsed = (month - source.fy_end_month - 1) % 12 + 1
        if elapsed % 3:
            return None
        year = int(match.group("year")) + (month > source.fy_end_month)
        if source.period_scheme == PeriodScheme.HALF_YEAR:
            return f"{year}H{elapsed // 6}" if elapsed in {6, 12} else None
        return f"{year}{('FQ' if source.period_scheme == PeriodScheme.FISCAL_Q else 'Q')}{elapsed // 3}"
    return _period_end_period(raw, source)


def canonical_period(raw: str, source: Source, event_type: EventType, event_date) -> str:
    if event_type not in {EventType.EARNINGS, EventType.EARNINGS_CALL}:
        return _canonical_non_earnings_period(raw, event_type, event_date)
    period = _match_earnings_period(raw, source)
    if period is None:
        stripped = _strip_period_noise(raw)
        if stripped and stripped != raw:
            period = _match_earnings_period(stripped, source)
    if period is None:
        period = _token_quarter_period(raw, source)
    if period is None:
        raise ValueError(f"period does not match configured scheme: {raw!r}")
    return period


_TARGET_PERIOD = re.compile("^(?P<year>20\\d{2})(?P<kind>FQ|Q|H)(?P<n>[1-4])$")
_QUARTER_TOKEN = re.compile(
    "\\bf?q(?P<a>[1-4])\\b|\\b(?P<b>[1-4])q(?=\\b|\\d)|\\bquarter (?P<c>[1-4])\\b|\\b(?P<word>first|second|third|fourth|1st|2nd|3rd|4th) quarter\\b"
)
_QUARTER_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "1st": 1,
    "2nd": 2,
    "3rd": 3,
    "4th": 4,
}
_NINE_MONTHS = re.compile("\\bq1 (?:to )?q3\\b|\\b(?:nine|9) months?\\b|\\b9m\\b")
_FIRST_HALF = re.compile(
    "\\bfirst half\\b|\\bhalf year(?:ly)?\\b|\\bhy\\b|\\bh1\\b|\\b1h\\b|\\binterim\\b|\\bsix months?\\b"
)
_SECOND_HALF = re.compile("\\bsecond half\\b|\\bh2\\b|\\b2h\\b")
_FULL_YEAR = re.compile(
    "\\bfull year\\b|\\bfiscal year\\b|\\bannual\\b|\\byear ended\\b|\\bfy(?:\\d{2}|\\d{4})?\\b|\\btwelve months\\b"
)


@dataclass(frozen=True)
class _PeriodTokens:
    compact: str
    years: frozenset[int]
    quarters: frozenset[int]
    nine: bool
    first_half: bool
    second_half: bool
    full_year: bool


def _period_tokens(raw: str) -> _PeriodTokens:
    compact = " ".join(re.sub("[-\\u2010\\u2011\\u2013\\u2014/,.()]", " ", raw.casefold()).split())
    years = {int(y) for y in re.findall("(?<!\\d)(20\\d{2})(?!\\d)", compact)}
    years |= {2000 + int(y) for y in re.findall("\\bfy ?(\\d{2})\\b", compact)}
    scan = _NINE_MONTHS.sub(" ", compact)
    quarters = {
        int(m.group("a") or m.group("b") or m.group("c"))
        if m.group("a") or m.group("b") or m.group("c")
        else _QUARTER_WORDS[m.group("word")]
        for m in _QUARTER_TOKEN.finditer(scan)
    }
    return _PeriodTokens(
        compact=compact,
        years=frozenset(years),
        quarters=frozenset(quarters),
        nine=bool(_NINE_MONTHS.search(compact)),
        first_half=bool(_FIRST_HALF.search(compact)),
        second_half=bool(_SECOND_HALF.search(compact)),
        full_year=bool(_FULL_YEAR.search(compact)),
    )


def _token_quarter_period(raw: str, source: Source) -> str | None:
    tokens = _period_tokens(raw)
    if len(tokens.years) != 1 or len(tokens.quarters) != 1:
        return None
    (year,), (quarter,) = (tokens.years, tokens.quarters)
    if tokens.nine and quarter != 3 or ((tokens.second_half or tokens.full_year) and quarter != 4):
        return None
    if source.period_scheme == PeriodScheme.FISCAL_Q:
        return f"{year}FQ{quarter}"
    if source.period_scheme == PeriodScheme.HALF_YEAR:
        half = {2: 1, 4: 2}.get(quarter)
        return f"{year}H{half}" if half else None
    return f"{year}Q{quarter}"


def _periods_equivalent(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    left = _TARGET_PERIOD.match(actual)
    right = _TARGET_PERIOD.match(expected)
    if left is None or right is None or left.group("year") != right.group("year"):
        return False
    pair = {
        (left.group("kind"), int(left.group("n"))),
        (right.group("kind"), int(right.group("n"))),
    }
    return pair in (
        {("H", 1), ("Q", 2)},
        {("H", 1), ("FQ", 2)},
        {("H", 2), ("Q", 4)},
        {("H", 2), ("FQ", 4)},
    )


def period_consistent(raw: str, source: Source, period: str) -> bool:
    target = _TARGET_PERIOD.match(period)
    tokens = _period_tokens(raw)
    compact, years, quarters = (tokens.compact, tokens.years, tokens.quarters)
    nine, first_half = (tokens.nine, tokens.first_half)
    second_half, full_year = (tokens.second_half, tokens.full_year)
    if re.fullmatch("20\\d{2}", compact):
        return False
    try:
        return _periods_equivalent(canonical_period(raw, source, EventType.EARNINGS, None), period)
    except ValueError:
        pass
    if not (quarters or nine or first_half or second_half or full_year):
        return False
    if target is None or years != {int(target.group("year"))}:
        return False
    kind, n = (target.group("kind"), int(target.group("n")))
    if kind == "H":
        if quarters:
            return quarters == {2 * n}
        if n == 1:
            return first_half and (not (second_half or full_year))
        return (second_half or full_year) and (not first_half)
    if quarters:
        return quarters == {n}
    if nine:
        return n == 3
    if first_half and (not (second_half or full_year)):
        return n == 2
    return n == 4 and (full_year or second_half)


_NON_EARNINGS_DEDUPE_TYPES = frozenset({"dividend", "agm", "cmd", "conference", "trading-update"})


def same_day_dedupe_key(
    event_type: str | EventType, period: str, event_date: object
) -> tuple[str, str, str] | None:
    """Return a deduplication bucket for same-day non-earnings aliases, preserving distinct subtypes."""
    kind = event_type.value if isinstance(event_type, EventType) else str(event_type)
    if kind not in _NON_EARNINGS_DEDUPE_TYPES:
        return None
    day = str(event_date)
    lowered = f"-{period.lower()}-"
    if kind == "agm":
        subtype = "egm" if period.lower().endswith("-egm") else "agm"
        return (kind, day, subtype)
    if kind == "dividend":
        div_class = "special" if "-special-" in lowered else "regular"
        milestone = (
            "ex"
            if "-ex-" in lowered
            else "record"
            if "-record-" in lowered
            else "pay"
            if "-pay-" in lowered
            else ""
        )
        return (kind, day, f"{div_class}:{milestone}")
    return (kind, day, "")


def event_id(event_type: EventType, ticker: str, period: str) -> str:
    return f"{event_type.value}:{ticker}:{period}"
