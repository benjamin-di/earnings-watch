from __future__ import annotations
from datetime import date
import pytest
from earnings_watch.model.events import EventType
from earnings_watch.model.sources import (
    FetchStrategy,
    PeriodScheme,
    Registry,
    Source,
    canonical_period,
)


def _source(**overrides) -> Source:
    values = {
        "key": "alpha",
        "issuer": "Alpha Components",
        "ticker": "ALPHA",
        "name": "ALPHA",
        "calendar_url": "https://example.com/calendar",
        "fetch": FetchStrategy.PLAIN,
        "period_scheme": PeriodScheme.CALENDAR_Q,
        "fy_end_month": 12,
    }
    values.update(overrides)
    return Source.model_validate(values)


@pytest.mark.parametrize(
    ("raw", "scheme", "expected"),
    [
        ("Q3 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("3Q 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("FY2027 Q2", PeriodScheme.FISCAL_Q, "2027FQ2"),
        ("FQ4 FY2026", PeriodScheme.FISCAL_Q, "2026FQ4"),
        ("1H 2026", PeriodScheme.HALF_YEAR, "2026H1"),
        ("2026 H2", PeriodScheme.HALF_YEAR, "2026H2"),
        ("H1 2026", PeriodScheme.CALENDAR_Q, "2026H1"),
        ("9M 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("FY 2026", PeriodScheme.CALENDAR_Q, "2026Q4"),
        ("FY25", PeriodScheme.CALENDAR_Q, "2025Q4"),
        ("FY2025 Annual Report", PeriodScheme.CALENDAR_Q, "2025Q4"),
        ("2025", PeriodScheme.CALENDAR_Q, "2025Q4"),
        ("Annual Results 2026", PeriodScheme.CALENDAR_Q, "2026Q4"),
        ("1Q26", PeriodScheme.CALENDAR_Q, "2026Q1"),
        ("3Q26", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("1st quarter 2026", PeriodScheme.CALENDAR_Q, "2026Q1"),
        ("2026 2nd Quarter", PeriodScheme.CALENDAR_Q, "2026Q2"),
        ("Third Quarter 2026", PeriodScheme.FISCAL_Q, "2026FQ3"),
        ("2026 Fourth Quarter", PeriodScheme.FISCAL_Q, "2026FQ4"),
        ("1H26", PeriodScheme.HALF_YEAR, "2026H1"),
        ("9M26", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("Full-year 2026", PeriodScheme.HALF_YEAR, "2026H2"),
        ("Q2 2026", PeriodScheme.HALF_YEAR, "2026H1"),
        ("First 9 Months 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("first 9 months of 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("Nine months of 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("First half 2026", PeriodScheme.CALENDAR_Q, "2026H1"),
        ("Second half of 2026", PeriodScheme.CALENDAR_Q, "2026H2"),
        ("2026 first half", PeriodScheme.HALF_YEAR, "2026H1"),
        ("Full year 2026 earnings - Press Conference", PeriodScheme.CALENDAR_Q, "2026Q4"),
        (
            "Earnings for the first 9 months of 2026- Press Conference",
            PeriodScheme.CALENDAR_Q,
            "2026Q3",
        ),
        ("First half 2026 earnings", PeriodScheme.CALENDAR_Q, "2026H1"),
        ("3rd Quarter (2019)", PeriodScheme.CALENDAR_Q, "2019Q3"),
        ("Press Conference on Full Year (2017) results", PeriodScheme.CALENDAR_Q, "2017Q4"),
        ("2026 Half-Year", PeriodScheme.CALENDAR_Q, "2026H1"),
        ("2026 Half-Year Report", PeriodScheme.CALENDAR_Q, "2026H1"),
        ("2nd quarter and first half 2026", PeriodScheme.CALENDAR_Q, "2026Q2"),
        ("3rd quarter and nine-month 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("4th quarter and full-year 2026", PeriodScheme.CALENDAR_Q, "2026Q4"),
        ("second quarter and first half 2026", PeriodScheme.HALF_YEAR, "2026H1"),
        ("Annual 2026", PeriodScheme.CALENDAR_Q, "2026Q4"),
        ("2026 Annual", PeriodScheme.CALENDAR_Q, "2026Q4"),
        ("interim statement as at 31 March 2026", PeriodScheme.CALENDAR_Q, "2026Q1"),
        ("half-yearly report as at 30 June 2026", PeriodScheme.CALENDAR_Q, "2026H1"),
        ("interim statement as at 30 September 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("annual report as at 31 December 2026", PeriodScheme.CALENDAR_Q, "2026Q4"),
        ("Interim 2026 (six months ended June 30, 2026)", PeriodScheme.HALF_YEAR, "2026H1"),
        ("2026 Interim Results", PeriodScheme.HALF_YEAR, "2026H1"),
        ("Interim Results 2026", PeriodScheme.HALF_YEAR, "2026H1"),
        ("2026 Interim Report", PeriodScheme.HALF_YEAR, "2026H1"),
        ("Interim Results for the six months ended 30 June 2026", PeriodScheme.HALF_YEAR, "2026H1"),
        (
            "INTERIM RESULTS ANNOUNCEMENT FOR THE SIX MONTHS ENDED 30 JUNE 2026",
            PeriodScheme.HALF_YEAR,
            "2026H1",
        ),
        ("Annual Results for the year ended 31 December 2025", PeriodScheme.HALF_YEAR, "2025H2"),
        ("Annual Results 2025", PeriodScheme.HALF_YEAR, "2025H2"),
        ("three months ended 31 March 2026", PeriodScheme.CALENDAR_Q, "2026Q1"),
        ("six months ended June 30, 2026", PeriodScheme.CALENDAR_Q, "2026H1"),
        ("nine months ended 30 September 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("twelve months ended 31 Dec 2026", PeriodScheme.CALENDAR_Q, "2026Q4"),
        ("Q3 2026 (three months ended September 30, 2026)", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("three months ended 30 June 2026", PeriodScheme.CALENDAR_Q, "2026Q2"),
        ("three months ended 31 December 2026", PeriodScheme.CALENDAR_Q, "2026Q4"),
        ("six months ended 31 December 2026", PeriodScheme.CALENDAR_Q, "2026H2"),
        ("2026 3Q", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("Quarter 3, 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("third-quarter 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("Interim Report on Operations – Third Quarter 2026", PeriodScheme.CALENDAR_Q, "2026Q3"),
        ("third-quarter fiscal 2027", PeriodScheme.FISCAL_Q, "2027FQ3"),
        ("fourth-quarter 2026", PeriodScheme.HALF_YEAR, "2026H2"),
        ("fourth-quarter and full-year results, 2026", PeriodScheme.CALENDAR_Q, "2026Q4"),
    ],
)
def test_period_canonicalizer_matches_live_source_key_grammar(
    raw: str, scheme: PeriodScheme, expected: str
) -> None:
    source = _source(period_scheme=scheme)
    assert canonical_period(raw, source, EventType.EARNINGS, date(2026, 7, 1)) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("half year ended 31 December 2025", "2026H1"),
        ("Results for the six months ended 31 December 2025", "2026H1"),
        ("year ended 30 June 2026", "2026H2"),
        ("financial year ended 30 June 2026", "2026H2"),
        ("half-year ended 30 June 2026", "2026H2"),
    ],
)
def test_period_end_idiom_resolves_against_fiscal_year_end(raw: str, expected: str) -> None:
    source = _source(period_scheme=PeriodScheme.HALF_YEAR, fy_end_month=6)
    assert canonical_period(raw, source, EventType.EARNINGS, date(2026, 8, 20)) == expected


@pytest.mark.parametrize(
    ("raw", "scheme", "fy_end_month"),
    [
        ("six months ended 30 September 2026", PeriodScheme.HALF_YEAR, 12),
        ("year ended 30 June 2026", PeriodScheme.HALF_YEAR, 12),
        ("nine months ended 30 September 2026", PeriodScheme.HALF_YEAR, 12),
        ("three months ended 31 March 2026", PeriodScheme.HALF_YEAR, 12),
        ("three months ended 31 May 2026", PeriodScheme.CALENDAR_Q, 12),
        ("three months ended September 3, 2026", PeriodScheme.FISCAL_Q, 8),
        ("Interim Report 2026", PeriodScheme.CALENDAR_Q, 12),
        ("Interim Report as of September 3, 2026", PeriodScheme.CALENDAR_Q, 12),
        ("third-quarter 2026 and fourth-quarter 2026 dates", PeriodScheme.CALENDAR_Q, 12),
        ("third-quarter 2026 versus third-quarter 2025", PeriodScheme.CALENDAR_Q, 12),
        ("third-quarter and full-year 2026", PeriodScheme.CALENDAR_Q, 12),
        ("third-quarter results", PeriodScheme.CALENDAR_Q, 12),
        ("third-quarter 2026", PeriodScheme.HALF_YEAR, 12),
    ],
)
def test_period_end_and_interim_idioms_reject_unsupported_shapes(
    raw: str, scheme: PeriodScheme, fy_end_month: int
) -> None:
    source = _source(period_scheme=scheme, fy_end_month=fy_end_month)
    with pytest.raises(ValueError, match="does not match configured scheme"):
        canonical_period(raw, source, EventType.EARNINGS, date(2026, 8, 20))


def test_half_year_scheme_rejects_non_boundary_quarter() -> None:
    with pytest.raises(ValueError, match="does not identify a half-year"):
        canonical_period(
            "Q3 2026",
            _source(period_scheme=PeriodScheme.HALF_YEAR),
            EventType.EARNINGS,
            date(2026, 7, 1),
        )


@pytest.mark.parametrize(
    ("raw", "event_type", "event_date", "expected"),
    [
        ("FY 2025 Dividend", EventType.DIVIDEND, date(2026, 5, 18), "2025-dividend"),
        ("FY2025 dividend", EventType.DIVIDEND, date(2026, 5, 18), "2025-dividend"),
        ("2025", EventType.DIVIDEND, date(2026, 6, 10), "2025-dividend"),
        (
            "FY2026 Interim Dividend",
            EventType.DIVIDEND,
            date(2026, 11, 23),
            "2026-interim-dividend",
        ),
        ("Interim Dividend 2026", EventType.DIVIDEND, date(2026, 11, 23), "2026-interim-dividend"),
        ("Special dividend", EventType.DIVIDEND, date(2026, 12, 1), "2026-special-dividend"),
        ("2027 AGM", EventType.AGM, date(2027, 4, 27), "2027"),
        ("2026 EGM", EventType.AGM, date(2026, 9, 10), "2026-egm"),
        ("2026 Extraordinary Shareholders Meeting", EventType.AGM, date(2026, 9, 10), "2026-egm"),
        ("Capital Markets Day", EventType.CMD, date(2026, 9, 2), "2026"),
    ],
)
def test_non_earnings_period_has_stable_semantic_identity(
    raw: str, event_type: EventType, event_date: date, expected: str
) -> None:
    assert canonical_period(raw, _source(), event_type, event_date) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [("key", "../escape"), ("key", "Uppercase"), ("ticker", "BARC LN"), ("ticker", "nvda")],
)
def test_source_identifiers_are_path_and_source_key_safe(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        _source(**{field: value})


@pytest.mark.parametrize(
    ("ignore", "match"), [(["  "], "non-empty"), (["Dup Plc", "Dup Plc "], "duplicate ignore")]
)
def test_registry_rejects_blank_or_duplicate_ignore_entries(ignore: list[str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Registry(sources=[_source()], ignore=ignore)
