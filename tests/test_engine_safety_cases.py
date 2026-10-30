"""Synthetic regressions for uncertainty, source disagreement and loss of authority."""

from datetime import date, datetime, timezone, timedelta
import pytest
from conftest import event, source
from earnings_watch.engine import timing, reconcile, verify
from earnings_watch.model.events import TimingEvidence, TimingKind, ExtractClaim

TODAY = date(2026, 10, 1)
DAY = date(2026, 11, 5)
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def basis(
    kind="publication",
    clock="08:00",
    url="https://issuer.example/calendar",
    evidence=None,
    withdrawn=False,
):
    return TimingEvidence(
        kind=kind,
        source_url=url,
        evidence=evidence
        or f"Q4 2026 results will be published on 5 November 2026 at {clock} UTC.",
        verified_on=TODAY,
        release_time=clock,
        release_tz="UTC",
        withdrawn=withdrawn,
    )


def timed(**kwargs):
    return event(date=DAY, release_time="08:00", release_tz="UTC", timing=basis(), **kwargs)


@pytest.mark.parametrize(
    "text,kind,label",
    [
        ("Board approval of Q4 results", TimingKind.BOARD_APPROVAL, "Board approval"),
        ("Q4 press conference", TimingKind.PRESS_CONFERENCE, "Press conference"),
        ("Q4 results published", TimingKind.PUBLICATION, "Release"),
    ],
)
def test_calendar_labels_distinguish_publication_from_approval_and_press(text, kind, label):
    row = event(date=DAY, evidence=text)
    assert timing.kind_for(row) == kind
    assert timing.timing_label(row).startswith(label)


def test_legacy_board_and_call_times_stay_unconfirmed():
    board = timing.with_legacy_evidence(
        event(
            date=DAY,
            evidence="Board approval of Q4 results",
            release_time="08:00",
            release_tz="UTC",
        )
    )
    assert board.status == "expected" and board.release_time is None
    call = timing.with_legacy_evidence(
        event(date=DAY, evidence="Q4 investor call at 08:00", time_hint="BMO")
    )
    assert call.status == "expected" and call.time_hint is None
    window = timing.with_legacy_evidence(
        event(date=DAY, evidence="Results released after market close", time_hint="AMC")
    )
    assert window.time_hint == "AMC" and window.timing.window_evidence


def test_publication_clock_survives_weaker_observations_but_not_withdrawal():
    old = timed()
    press = timed().model_copy(
        update={"release_time": "09:00", "timing": basis(kind="press-conference", clock="09:00")}
    )
    assert timing.merge_observation(old, press).release_time == "08:00"
    untimed = event(date=DAY)
    assert timing.merge_observation(old, untimed).release_time == "08:00"
    withdrawn = untimed.model_copy(update={"timing": basis(withdrawn=True)})
    assert timing.merge_observation(old, withdrawn).release_time is None
    secondary = event(date=DAY + timedelta(days=1), status="secondary")
    merged = timing.merge_observation(old, secondary)
    assert merged.date == DAY and merged.timing_issue == timing.DATE_DISAGREEMENT


def test_time_dispute_is_durable_and_correction_is_explicit():
    old = timed()
    incoming = timed().model_copy(
        update={
            "release_time": "09:00",
            "timing": basis(clock="09:00", url="https://issuer.example/other"),
        }
    )
    merged = timing.merge_observation(old, incoming)
    assert merged.release_time == "08:00" and merged.timing_issue
    assert len(merged.timing_disputes) == 2
    corrected = incoming.model_copy(
        update={
            "timing": basis(
                clock="09:00", evidence="Corrected time: results now published at 09:00 UTC"
            )
        }
    )
    assert timing.explicit_correction(corrected)
    assert timing.merge_observation(old, corrected).release_time == "09:00"
    unresolved = old.model_copy(
        update={
            "release_time": None,
            "release_tz": None,
            "timing_issue": "Conflicting timings",
            "timing_disputes": [basis(), basis(clock="09:00")],
        }
    )
    assert timing.merge_observation(unresolved, incoming).release_time is None
    assert timing.coverage([old, unresolved, event(date=DAY)]) == {
        "confirmed": 1,
        "review": 1,
        "unknown": 1,
    }


def test_normalized_clock_and_fingerprint_require_evidence():
    row = timed()
    assert timing.clock_key(row).hour == 8
    assert timing.clock_key(event(date=DAY)) is None
    assert timing.timing_candidate_fingerprint(DAY, basis()).startswith("sha256:")
    no_clock = TimingEvidence(
        kind="publication", source_url=row.source_url, evidence="Date only", verified_on=TODAY
    )
    with pytest.raises(ValueError, match="no normalized clock"):
        timing.timing_candidate_fingerprint(DAY, no_clock)
    assert len(timing.merge_timing_disputes(DAY, [no_clock, no_clock, basis()])) == 2


@pytest.mark.parametrize(
    "old,new,text",
    [
        (event(date=DAY), timed(), "Time confirmed"),
        (timed(), event(date=DAY), "Exact time withdrawn"),
        (timed(), timed().model_copy(update={"release_time": "09:00"}), "Time changed"),
        (event(date=DAY), event(date=DAY, time_hint="AMC"), "Release window now AMC"),
        (event(date=DAY, time_hint="AMC"), event(date=DAY), "Release window no longer stated"),
        (event(date=DAY), event(date=DAY), "Timing basis changed"),
    ],
)
def test_retime_diagnostics_explain_material_change(old, new, text):
    assert reconcile.retime_reason(old, new).startswith(text)
    assert reconcile.timing_desc(new)


def test_source_demotion_and_call_derivation_never_invent_publication():
    old = event(date=DAY)
    downgraded, counts = reconcile.downgrade_ir_confirmed((old,), {"alpha"}, today=TODAY, now=NOW)
    assert counts == {"alpha": 1} and downgraded[0].status == "secondary"
    assert downgraded[0].history[-1].action == "downgraded"
    retained, counts = reconcile.downgrade_ir_confirmed(
        (old,), {"alpha"}, today=TODAY, now=NOW, retain_ids={old.id}
    )
    assert retained == (old,) and not counts
    call = event(date=DAY, type="earnings-call", id="earnings-call:alpha:2026Q4")
    derived = reconcile.results_from_calls([call], [], today=TODAY)
    assert derived[0].status == "expected" and derived[0].timing_issue == timing.CALL_ONLY_RESULTS
    assert not derived[0].release_time
    assert not reconcile.results_from_calls([call], [old], today=TODAY)


def test_timing_candidates_must_be_quoted_in_their_own_primary_document():
    src = source()
    quote = "Q4 2026 results will be published on 5 November 2026 at 08:00 UTC."
    row = timed(evidence=quote)
    checked = verify.check_timing_candidates([row], src, {row.source_url: quote})
    assert checked and all(checked.values())
    rejected = verify.check_timing_candidates([row], src, {row.source_url: "No quoted result here"})
    assert rejected and not any(rejected.values())
    assert verify.reverify_event(row, quote, today=TODAY)
    assert verify.reverify_event(row, "No evidence", today=TODAY) is None


def test_conflicting_verified_candidates_keep_event_all_day():
    src = source()
    first = "Q4 2026 results will be published on 5 November 2026 at 08:00 UTC."
    second = "Q4 2026 results will be published on 5 November 2026 at 09:00 UTC."
    other = "https://issuer.example/other"
    claim = ExtractClaim(
        type="earnings",
        period="Q4 2026",
        date=DAY,
        evidence=first,
        timing_candidates=[
            {
                "source_url": src.calendar_url,
                "evidence": first,
                "release_time": "08:00",
                "release_tz": "UTC",
            },
            {"source_url": other, "evidence": second, "release_time": "09:00", "release_tz": "UTC"},
        ],
    )
    result = verify.verify_claims(
        [claim], src, first, today=TODAY, documents={src.calendar_url: first, other: second}
    )
    assert not result.rejected
    assert result.events[0].release_time is None
    assert result.events[0].timing_issue and len(result.events[0].timing_disputes) == 2
