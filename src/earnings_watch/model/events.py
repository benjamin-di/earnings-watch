from __future__ import annotations
import re
from datetime import date as Date
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_RELEASE_TIME_RE = re.compile("^([01]\\d|2[0-3]):[0-5]\\d$")
_SHA256_RE = re.compile("^sha256:[0-9a-f]{64}$")


class EventType(StrEnum):
    EARNINGS = "earnings"
    EARNINGS_CALL = "earnings-call"
    AGM = "agm"
    CMD = "cmd"
    DIVIDEND = "dividend"


class EventStatus(StrEnum):
    IR_CONFIRMED = "ir-confirmed"
    SECONDARY = "secondary"
    EXPECTED = "expected"
    PAST = "past"


class TimeHint(StrEnum):
    BMO = "BMO"
    AMC = "AMC"
    DURING = "DURING"


class TimingKind(StrEnum):
    PUBLICATION = "publication"
    PRESS_CONFERENCE = "press-conference"
    INVESTOR_CALL = "investor-call"
    BOARD_APPROVAL = "board-approval"


class ReleaseTimeMixin(BaseModel):
    release_time: str | None = None
    release_tz: str | None = None

    @field_validator("release_time")
    @classmethod
    def _valid_release_time(cls, value: str | None) -> str | None:
        if value is not None and (not _RELEASE_TIME_RE.match(value)):
            raise ValueError("release_time must be 24h HH:MM")
        return value

    @field_validator("release_tz")
    @classmethod
    def _valid_release_tz(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"release_tz is not an IANA zone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _release_pair(self):
        if (self.release_time is None) != (self.release_tz is None):
            raise ValueError("release_time and release_tz must be set together")
        return self


class TimingCandidate(ReleaseTimeMixin):
    model_config = ConfigDict(extra="forbid")
    release_time: str
    release_tz: str
    source_url: str
    evidence: str

    @field_validator("source_url", "evidence")
    @classmethod
    def _candidate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("timing candidate evidence and source URL must be non-empty")
        return value


class TimingEvidence(ReleaseTimeMixin):
    model_config = ConfigDict(extra="forbid")
    kind: TimingKind
    source_url: str
    evidence: str
    verified_on: Date
    withdrawn: bool = False
    window_evidence: str | None = None
    timezone_basis: str | None = None

    @field_validator("source_url", "evidence")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("timing evidence and source URL must be non-empty")
        return value


class TimingResolution(TimingCandidate):
    kind: TimingKind
    candidate_fingerprint: str
    reviewed_on: Date
    reason: str

    @field_validator("candidate_fingerprint")
    @classmethod
    def _fingerprint(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("timing candidate fingerprint must be sha256:<hex>")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("timing resolution reason must be non-empty")
        return value


class TimingSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    select_candidate: str
    reviewed_on: Date
    reason: str

    @field_validator("select_candidate")
    @classmethod
    def _selection_fingerprint(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("selected timing candidate must be sha256:<hex>")
        return value

    @field_validator("reason")
    @classmethod
    def _selection_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("timing selection reason must be non-empty")
        return value


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: datetime
    action: str
    old_date: Date | None = None
    new_date: Date | None = None
    old_status: EventStatus | None = None
    new_status: EventStatus | None = None
    note: str = ""


class ResultsLinkKind(StrEnum):
    PRESS_RELEASE = "press-release"
    PRESENTATION = "presentation"
    SHAREHOLDER_LETTER = "shareholder-letter"
    FILING = "filing"
    FINANCIAL_STATEMENTS = "financial-statements"
    EXCHANGE_ANNOUNCEMENT = "exchange-announcement"
    WEBCAST = "webcast"
    OTHER = "other"


RESULTS_LINK_TITLE_MAX_CHARS = 120


class ResultsLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    kind: ResultsLinkKind
    url: str
    evidence_url: str | None = None
    evidence: str = ""
    coverage: list[str] = Field(default_factory=list)
    content_sha256: str | None = None
    attachment_path: str | None = None
    verified_at: datetime | None = None
    unverified: str | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("title must be non-empty")
        return value[:RESULTS_LINK_TITLE_MAX_CHARS]

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("https://"):
            raise ValueError("results link must be https")
        if any((ch.isspace() or ord(ch) < 32 for ch in value)):
            raise ValueError("results link must not contain whitespace")
        if not urlsplit(value).hostname:
            raise ValueError("results link must have a host")
        return value


class Event(ReleaseTimeMixin):
    model_config = ConfigDict(extra="forbid")
    id: str
    issuer_key: str
    ticker: str
    type: EventType
    period: str
    date: Date
    time_hint: TimeHint | None = None
    timing: TimingEvidence | None = None
    timing_issue: str | None = None
    timing_disputes: list[TimingEvidence] = Field(default_factory=list, max_length=8)
    timing_resolution: TimingResolution | None = None
    status: EventStatus
    source_url: str
    evidence: str
    first_seen: Date
    last_verified: Date
    history: list[HistoryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _timing_matches_event(self):
        if self.timing is not None and (self.type == EventType.EARNINGS_CALL) != (
            self.timing.kind == TimingKind.INVESTOR_CALL
        ):
            raise ValueError("investor-call timing requires a separate earnings-call event")
        if self.timing_resolution is not None:
            if not self.timing_issue:
                raise ValueError("reviewed timing resolution requires a timing issue")
            if (
                self.release_time != self.timing_resolution.release_time
                or self.release_tz != self.timing_resolution.release_tz
            ):
                raise ValueError("event clock must match its reviewed timing resolution")
        return self

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if value.count(":") != 2 or any((not part for part in value.split(":"))):
            raise ValueError("event id must be type:source_key:period")
        return value


class ExtractClaim(ReleaseTimeMixin):
    model_config = ConfigDict(extra="forbid")
    type: EventType
    period: str
    date: Date
    time_hint: TimeHint | None = None
    timing_kind: TimingKind | None = None
    timing_candidates: list[TimingCandidate] = Field(default_factory=list, max_length=4)
    timing_conflict: bool = False
    evidence: str
    source_url: str | None = None

    @field_validator("period", "evidence")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must be non-empty")
        return value


class ExtractResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_relevant: bool
    complete: bool
    events: list[ExtractClaim]


class ResultsLinkClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    kind: str
    url: str
    snapshot_id: str = ""
    evidence: str = ""
    period: str = ""
    coverage: dict[str, str] = Field(default_factory=dict)


class ResultsSourceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    evidence: str


class ResultsLinksResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    released: bool
    complete: bool
    docs: list[ResultsLinkClaim]
    checks: list[ResultsSourceCheck] = Field(default_factory=list, max_length=16)


class RejectedClaim(BaseModel):
    claim: ExtractClaim
    reason: str


class SourceRunStats(BaseModel):
    key: str
    http_status: int | None = None
    fetch_state: str
    content_sha256: str | None = None
    n_events: int = 0
    n_verified: int = 0
    n_rejected: int = 0
    n_merged: int = 0
    n_retained: int = 0


class HealthEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_ok: datetime | None = None
    last_state: str
    consecutive_failures: int = 0
    state_streak: int = 1
    page_sha256: str | None = None
    extraction_prompt_sha256: str | None = None
    search_last_attempt: datetime | None = None
    search_last_ok: datetime | None = None
    search_failures: int = 0
    search_checked_periods: list[str] = Field(default_factory=list)
