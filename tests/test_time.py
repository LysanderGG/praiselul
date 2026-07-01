from datetime import datetime
from zoneinfo import ZoneInfo

from praiselul.config import Config
from praiselul.duration import Duration
from praiselul.time import (
    LeaveTime,
    _current_day_worked_minutes,
    _day_actual_minutes,
    get_latest_clock_in_time,
    get_leave_time,
    get_overtime_balance,
    get_overtime_history,
    get_workplace_times,
)

DEFAULT_CONFIG = Config(praise_url="", praise_email="", praise_password="")  # 8h/day
PART_TIME_CONFIG = Config(praise_url="", praise_email="", praise_password="", hours_per_day=6)

# Use UTC in tests so clock-in timestamps don't need offset adjustment
TZ = ZoneInfo("UTC")


def _make_day(
    date: str,
    day_type: str = "working_day",
    actual_work_minutes: int | None = None,
    clock_in: str | None = None,
    clock_out: str | None = None,
    break_minutes: int | None = None,
    sessions: list | None = None,
) -> dict:
    return {
        "date": date,
        "dayType": day_type,
        "actualWorkMinutes": actual_work_minutes,
        "expectedMinutes": 480,  # Praise always sends this; we ignore it for overtime calc
        "clockIn": clock_in,
        "clockOut": clock_out,
        "breakMinutes": break_minutes,
        "sessions": sessions or [],
    }


# --- get_overtime_history ---


def test_overtime_history_normal_days():
    days = [
        _make_day("2026-04-07", actual_work_minutes=525),  # 8:45 worked, 8:00 expected → +45
        _make_day("2026-04-08", actual_work_minutes=505),  # 8:25 → +25
        _make_day("2026-04-09", actual_work_minutes=432),  # 7:12 → -48
    ]
    labels, history = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert labels == ["4/7(火)", "4/8(水)", "4/9(木)"]
    assert history == [Duration(45), Duration(25), Duration(-48)]


def test_overtime_history_skips_rest_days_with_no_activity():
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),
        _make_day("2026-04-08", day_type="statutory_rest_day", actual_work_minutes=None),
        _make_day("2026-04-09", actual_work_minutes=480),
    ]
    labels, history = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert labels == ["4/7(火)", "4/9(木)"]
    assert history == [Duration(0), Duration(0)]


def test_overtime_history_worked_holiday():
    """Working on a holiday (expected=0 via dayType) counts all hours as overtime."""
    days = [
        _make_day("2026-04-08", actual_work_minutes=480),
        _make_day("2026-04-09", day_type="holiday", actual_work_minutes=203),  # 3:23 worked, 0 expected
    ]
    labels, history = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert labels == ["4/8(水)", "4/9(木)"]
    assert history == [Duration(0), Duration(203)]


def test_overtime_history_worked_rest_day():
    """Working on a rest day counts all hours as overtime (expected=0)."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),
        _make_day("2026-04-08", day_type="scheduled_rest_day", actual_work_minutes=180),  # 3h worked
    ]
    _, history = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert history == [Duration(0), Duration(180)]


def test_overtime_history_part_time():
    """Part-time: config.hours_per_day=6 means expected=360 per working day."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=494),  # 8:14 - 6:00 = +134
        _make_day("2026-04-08", actual_work_minutes=488),  # 8:08 - 6:00 = +128
    ]
    _, history = get_overtime_history(days, PART_TIME_CONFIG, TZ)
    assert history == [Duration(134), Duration(128)]


# --- leave days ---


def _make_leave_day(
    date: str,
    leave_unit: str,
    category: str = "paid",
    actual_work_minutes: int | None = None,
) -> dict:
    """A working_day carrying leave metadata (leave is encoded via
    leaveCategory/leaveUnit, not as a separate dayType)."""
    day = _make_day(date, actual_work_minutes=actual_work_minutes)
    day["leaveType"] = "Paid Leave"
    day["leaveUnit"] = leave_unit
    day["leaveCategory"] = category
    return day


def test_overtime_history_full_day_paid_leave_is_neutral():
    """A full paid-leave day expects 0, so it neither adds nor removes overtime.
    (Regression: it used to count as worked-0 / expected-8h = -8h.)"""
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),  # exactly 8h → 0
        _make_leave_day("2026-04-08", "full_day"),  # paid full-day leave → neutral
        _make_day("2026-04-09", actual_work_minutes=480),  # 0
    ]
    labels, _ = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert labels == ["4/7(火)", "4/9(木)"]  # leave day has no activity → skipped
    assert get_overtime_balance(days, DEFAULT_CONFIG, TZ) == Duration(0)


def test_overtime_history_half_day_paid_leave():
    """A half paid-leave day expects half the hours; the rest is real overtime."""
    days = [_make_leave_day("2026-04-08", "half_day_am", actual_work_minutes=300)]  # 5h worked, 4h expected
    _, history = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert history == [Duration(60)]


def test_overtime_history_unpaid_leave_still_owes_full_hours():
    """Unpaid leave does not reduce expected hours — the shortfall is intentional."""
    days = [_make_leave_day("2026-04-08", "full_day", category="unpaid")]
    _, history = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert history == [Duration(-480)]


def test_overtime_history_unknown_paid_leave_unit_defers_to_expected_minutes():
    """An unrecognized paid-leave unit is not assumed to be a half day — it falls
    back to the timesheet's reported expectedMinutes."""
    day = _make_leave_day("2026-04-08", "hourly", actual_work_minutes=360)
    day["expectedMinutes"] = 360  # server already subtracted the leave
    _, history = get_overtime_history([day], DEFAULT_CONFIG, TZ)
    assert history == [Duration(0)]  # 360 worked - 360 expected, NOT 360 - 240


def test_overtime_history_non_unpaid_category_still_credits_hours():
    """Any category other than unpaid credits the day's hours — gating off
    "unpaid" (not an exact "paid" match) keeps a future paid-type category from
    reintroducing the phantom shortfall."""
    days = [_make_leave_day("2026-04-08", "full_day", category="special")]
    # Full day expects 0 → neutral; under an exact "paid" match it was -8h.
    assert get_overtime_balance(days, DEFAULT_CONFIG, TZ) == Duration(0)


# --- pending (unapproved) leave requests ---


def _make_pending_leave_day(
    date: str,
    usage: str,
    category: str = "paid",
    actual_work_minutes: int | None = None,
) -> dict:
    """A working day carrying a *pending* leave request, as Praise attaches them
    to the timesheet day (separate from the approved leaveCategory/leaveUnit)."""
    day = _make_day(date, actual_work_minutes=actual_work_minutes)
    day["pendingLeaveRequests"] = [
        {"id": "req-1", "leaveTypeName": "Paid Leave", "leaveTypeCategory": category, "usage": usage}
    ]
    return day


def test_pending_full_day_paid_leave_is_neutral():
    """A pending full-day paid request expects 0, so an empty day doesn't read as
    a full shortfall while it awaits approval."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),
        _make_pending_leave_day("2026-04-08", "full_day"),  # requested off, not yet approved
        _make_day("2026-04-09", actual_work_minutes=480),
    ]
    # Without folding in pending leave this day would be 0 - 480 = -480.
    assert get_overtime_balance(days, DEFAULT_CONFIG, TZ) == Duration(0)


def test_pending_half_day_paid_leave_expects_half():
    """A pending half-day paid request expects half the hours; the rest is real overtime."""
    days = [_make_pending_leave_day("2026-04-08", "half_day_pm", actual_work_minutes=300)]  # 5h worked, 4h expected
    _, history = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert history == [Duration(60)]


def test_pending_unpaid_leave_still_owes_full_hours():
    """Pending *unpaid* leave does not reduce expected hours — same as approved unpaid."""
    days = [_make_pending_leave_day("2026-04-08", "full_day", category="unpaid")]
    _, history = get_overtime_history(days, DEFAULT_CONFIG, TZ)
    assert history == [Duration(-480)]


def test_pending_am_and_pm_halves_cover_a_full_day():
    """Two pending half-day requests (AM + PM) add up to a full day off → expects 0."""
    day = _make_day("2026-04-08")  # no work logged
    day["pendingLeaveRequests"] = [
        {"id": "a", "leaveTypeCategory": "paid", "usage": "half_day_am"},
        {"id": "b", "leaveTypeCategory": "paid", "usage": "half_day_pm"},
    ]
    assert get_overtime_balance([day], DEFAULT_CONFIG, TZ) == Duration(0)


def test_pending_leave_never_increases_expected():
    """Pending leave can only lower the expectation: an approved full-day (expects 0)
    with a stray pending half on top stays 0, not base/2."""
    day = _make_leave_day("2026-04-08", "full_day")  # approved full day → expects 0
    day["pendingLeaveRequests"] = [{"id": "x", "leaveTypeCategory": "paid", "usage": "half_day_am"}]
    assert get_overtime_balance([day], DEFAULT_CONFIG, TZ) == Duration(0)


def test_pending_leave_on_non_working_day_is_ignored():
    """A pending request on a rest day changes nothing — the day already expects 0,
    so working it is still all overtime."""
    day = _make_pending_leave_day("2026-04-08", "full_day", actual_work_minutes=120)
    day["dayType"] = "statutory_rest_day"
    _, history = get_overtime_history([day], DEFAULT_CONFIG, TZ)
    assert history == [Duration(120)]


def test_approved_half_and_pending_complementary_half_cover_full_day():
    """Approved and pending coverage sum: an approved AM half plus a pending PM
    half cover the whole day → expects 0, not base/2."""
    day = _make_leave_day("2026-04-08", "half_day_am")  # approved AM half → 0.5
    day["pendingLeaveRequests"] = [{"id": "x", "leaveTypeCategory": "paid", "usage": "half_day_pm"}]
    assert get_overtime_balance([day], DEFAULT_CONFIG, TZ) == Duration(0)


def test_pending_non_unpaid_category_still_credits_hours():
    """Like approved leave, pending leave gates off "unpaid" rather than an exact
    "paid" match, so a future paid-type category still credits the hours."""
    day = _make_pending_leave_day("2026-04-08", "full_day", category="special")
    assert get_overtime_balance([day], DEFAULT_CONFIG, TZ) == Duration(0)


def test_leave_time_pending_half_day_today_targets_remaining_hours():
    """A pending half-day request *today* reduces today's target just like an
    approved one, so 'when to leave' reflects requested-but-unapproved leave."""
    today = _make_open_day("2026-04-08", clock_in="2026-04-08T13:00:00Z")
    today["pendingLeaveRequests"] = [{"id": "x", "leaveTypeCategory": "paid", "usage": "half_day_am"}]
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),  # 0 balance
        today,
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # required_today = 240 (half day) - 0 = 240 (<5h) → no break; leave = 13:00 + 240 = 17:00.
    assert leave_times == [
        LeaveTime(includes_break=False, min_time=Duration.parse("17:00")),
    ]


# --- get_overtime_balance ---


def test_overtime_balance():
    days = [
        _make_day("2026-04-07", actual_work_minutes=525),  # +45
        _make_day("2026-04-08", actual_work_minutes=505),  # +25
        _make_day("2026-04-09", actual_work_minutes=432),  # -48
    ]
    assert get_overtime_balance(days, DEFAULT_CONFIG, TZ) == Duration(22)


# --- get_workplace_times ---


def test_workplace_times():
    summary = {"onSiteMinutes": 2400, "remoteMinutes": 600}
    result = get_workplace_times(summary)
    assert result == {"On-site": Duration(2400), "Remote": Duration(600)}


def test_workplace_times_no_remote():
    summary = {"onSiteMinutes": 2400, "remoteMinutes": 0}
    result = get_workplace_times(summary)
    assert result == {"On-site": Duration(2400)}


# --- get_leave_time ---


def _make_open_day(date: str, clock_in: str) -> dict:
    """Make a day with an open session (clocked in, not yet clocked out)."""
    return _make_day(
        date=date,
        actual_work_minutes=None,
        clock_in=clock_in,
        sessions=[{"clockIn": clock_in, "clockOut": None}],
    )


def test_leave_time_with_break():
    """Required > 6h → single window with break."""
    days = [
        # Previous day: 9 min overtime → required_today = 480-9 = 471 (>360) → break
        _make_day("2026-04-07", actual_work_minutes=489),
        _make_open_day("2026-04-08", clock_in="2026-04-08T09:00:00Z"),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # leave = 09:00 + 471min + 60min break = 17:51
    assert leave_times == [
        LeaveTime(includes_break=True, min_time=Duration.parse("17:51")),
    ]


def test_leave_time_no_break():
    """Required < 5h → single window without break."""
    days = [
        # Previous days: +200min overtime → required_today = 480-200 = 280 (<300) → no break
        _make_day("2026-04-07", actual_work_minutes=680),
        _make_open_day("2026-04-08", clock_in="2026-04-08T09:00:00Z"),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # leave = 09:00 + 280min = 13:40
    assert leave_times == [
        LeaveTime(includes_break=False, min_time=Duration.parse("13:40")),
    ]


def test_double_leave_time():
    """Required 5-6h → two windows."""
    days = [
        # Previous days: +150min overtime → required_today = 480-150 = 330 (between 300 and 360)
        _make_day("2026-04-07", actual_work_minutes=630),
        _make_open_day("2026-04-08", clock_in="2026-04-08T09:00:00Z"),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    assert leave_times == [
        LeaveTime(includes_break=False, min_time=Duration.parse("14:30"), max_time=Duration.parse("15:00")),
        LeaveTime(includes_break=True, min_time=Duration.parse("15:30")),
    ]


def test_leave_time_negative_overtime():
    """Negative overtime balance means more hours required today."""
    days = [
        # Previous day: -30min overtime → required_today = 480+30 = 510 (>360) → break
        _make_day("2026-04-07", actual_work_minutes=450),
        _make_open_day("2026-04-08", clock_in="2026-04-08T09:00:00Z"),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # leave = 09:00 + 510min + 60min = 18:30
    assert leave_times == [
        LeaveTime(includes_break=True, min_time=Duration.parse("18:30")),
    ]


def test_leave_time_already_clocked_out():
    """Already clocked out → NoClockInError caught by CLI."""
    from praiselul.errors import NoClockInError
    import pytest

    days = [
        _make_day(
            "2026-04-08",
            actual_work_minutes=480,
            clock_in="2026-04-08T09:00:00Z",
            clock_out="2026-04-08T18:00:00Z",
            sessions=[{"clockIn": "2026-04-08T09:00:00Z", "clockOut": "2026-04-08T18:00:00Z"}],
        ),
    ]
    with pytest.raises(NoClockInError):
        get_leave_time(days, DEFAULT_CONFIG, TZ)


def test_leave_time_part_time():
    """Part-time config: hours_per_day=6 changes the target."""
    days = [
        # Previous day: exactly 6h → 0 overtime → required_today = 360
        _make_day("2026-04-07", actual_work_minutes=360),
        _make_open_day("2026-04-08", clock_in="2026-04-08T09:00:00Z"),
    ]
    leave_times = get_leave_time(days, PART_TIME_CONFIG, TZ)
    # required_today = 360 = 6h exactly → 5-6h range (double window)
    assert leave_times == [
        LeaveTime(includes_break=False, min_time=Duration.parse("15:00"), max_time=Duration.parse("15:00")),
        LeaveTime(includes_break=True, min_time=Duration.parse("16:00")),
    ]


def test_leave_time_with_timezone():
    """Clock-in in UTC should be converted to local time for departure calc."""
    jst = ZoneInfo("Asia/Tokyo")
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),  # 0 overtime
        # Clock-in at 2026-04-08T00:00:00Z = 09:00 JST
        _make_open_day("2026-04-08", clock_in="2026-04-08T00:00:00Z"),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, jst)
    # required_today = 480 (>360) → break
    # leave = 09:00 JST + 480min + 60min = 18:00
    assert leave_times == [
        LeaveTime(includes_break=True, min_time=Duration.parse("18:00")),
    ]


def test_leave_time_half_day_leave_today_targets_remaining_hours():
    """A half-day leave *today* only requires the remaining half, so the target is
    4h (not the full 8h) before adjusting for the prior balance."""
    today = _make_open_day("2026-04-08", clock_in="2026-04-08T13:00:00Z")
    today["leaveType"] = "Paid Leave"
    today["leaveUnit"] = "half_day_am"
    today["leaveCategory"] = "paid"
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),  # 0 balance
        today,
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # required_today = 240 (half day) - 0 = 240 (<5h) → no break; leave = 13:00 + 240 = 17:00.
    # (Without the leave adjustment it would target 8h → break → 21:00.)
    assert leave_times == [
        LeaveTime(includes_break=False, min_time=Duration.parse("17:00")),
    ]


# --- _current_day_worked_minutes (live computation for an open/current day) ---

# Fixed "now" = 16:00 UTC on the open day, so elapsed time is deterministic.
NOW = datetime(2026, 4, 8, 16, 0, tzinfo=TZ)


def _session(clock_in: str, clock_out: str | None = None, **extra) -> dict:
    return {"clockIn": clock_in, "clockOut": clock_out, **extra}


def test_current_day_single_session_over_6h_applies_auto_break():
    """One continuous open session ≥6h with no recorded break → subtract the mandatory hour."""
    # 09:00 → 16:00 = 7h gross, no break → 420 - 60 = 360
    day = _make_open_day("2026-04-08", clock_in="2026-04-08T09:00:00Z")
    assert _current_day_worked_minutes(day, NOW, TZ) == 360


def test_current_day_single_session_at_6h_boundary_applies_auto_break():
    """Exactly 6h hits the threshold (>=) → auto-break applies, matching RecoLul."""
    # 10:00 → 16:00 = 6h = 360, no break → 360 - 60 = 300
    day = _make_open_day("2026-04-08", clock_in="2026-04-08T10:00:00Z")
    assert _current_day_worked_minutes(day, NOW, TZ) == 300


def test_current_day_single_session_under_6h_no_break():
    """One continuous open session <6h → no deduction."""
    # 11:00 → 16:00 = 5h = 300
    day = _make_open_day("2026-04-08", clock_in="2026-04-08T11:00:00Z")
    assert _current_day_worked_minutes(day, NOW, TZ) == 300


def test_current_day_clock_in_out_in_no_auto_break():
    """User clocked in/out/in (a real break taken) → no auto-break even if total ≥6h."""
    # closed 09:00–13:00 (4h) + open 14:00–16:00 (2h). Neither session ≥6h.
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=None,
        clock_in="2026-04-08T09:00:00Z",
        sessions=[
            _session("2026-04-08T09:00:00Z", "2026-04-08T13:00:00Z", actualWorkMinutes=240),
            _session("2026-04-08T14:00:00Z", None),
        ],
    )
    # 240 (closed, trusted) + 120 (open, <6h, no break) = 360 — NOT 360-60.
    assert _current_day_worked_minutes(day, NOW, TZ) == 360


def test_current_day_trusts_backend_actual_for_closed_sessions():
    """Closed sessions use the backend's already-break-adjusted actualWorkMinutes verbatim."""
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=None,
        clock_in="2026-04-08T09:00:00Z",
        sessions=[
            # Backend says 150 (e.g. 3h gross minus a recorded 30-min break), not the 180 gross.
            _session("2026-04-08T09:00:00Z", "2026-04-08T12:00:00Z", actualWorkMinutes=150, breakMinutes=30),
            _session("2026-04-08T13:00:00Z", None),
        ],
    )
    # 150 (trusted) + 180 (open 13:00–16:00, <6h) = 330
    assert _current_day_worked_minutes(day, NOW, TZ) == 330


def test_current_day_recorded_break_suppresses_auto_break():
    """A break recorded within the open session is used instead of the mandatory hour."""
    # 09:00 → 16:00 = 7h gross, with a recorded 30-min break → 420 - 30 = 390 (not 420 - 60).
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=None,
        clock_in="2026-04-08T09:00:00Z",
        sessions=[
            _session(
                "2026-04-08T09:00:00Z",
                None,
                breakPeriods=[
                    {"start": "2026-04-08T12:00:00Z", "end": "2026-04-08T12:30:00Z", "minutes": 30}
                ],
            ),
        ],
    )
    assert _current_day_worked_minutes(day, NOW, TZ) == 390


# --- office-then-remote: a closed on-site session followed by an open remote one ---
#
# Praise summarises the day with the *first* session's clock-in and the
# *closed* sessions' minutes, so reading those day-level fields makes `balance`
# freeze the remote session's elapsing time and `when` anchor on the morning
# office clock-in. These cover both.


def _make_office_then_remote_day(
    date: str,
    office_in: str,
    office_out: str,
    office_minutes: int,
    remote_in: str,
) -> dict:
    """A day with a clocked-out on-site session and a still-running remote one.

    The day-level fields mirror Praise: ``clockIn`` is the office clock-in and
    ``actualWorkMinutes`` reflects only the closed office session.
    """
    return _make_day(
        date=date,
        actual_work_minutes=office_minutes,
        clock_in=office_in,
        clock_out=None,
        sessions=[
            _session(office_in, office_out, actualWorkMinutes=office_minutes),
            _session(remote_in, None),
        ],
    )


def test_day_actual_minutes_open_session_overrides_stale_day_total():
    """Bug 1: the open remote session keeps accruing even after Praise froze the
    day-level actualWorkMinutes from the closed on-site session."""
    day = _make_office_then_remote_day(
        "2026-04-08",
        office_in="2026-04-08T09:00:00Z",
        office_out="2026-04-08T12:00:00Z",
        office_minutes=180,  # stale day total: office only
        remote_in="2026-04-08T13:00:00Z",
    )
    # 180 (office) + 180 (remote 13:00→16:00, <6h, no break) = 360, NOT the stale 180.
    assert _day_actual_minutes(day, TZ, now=NOW) == 360


def test_day_actual_minutes_closed_day_trusts_backend_total():
    """A fully clocked-out day still trusts Praise's day-level actualWorkMinutes."""
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=455,
        clock_in="2026-04-08T09:00:00Z",
        clock_out="2026-04-08T17:35:00Z",
        sessions=[_session("2026-04-08T09:00:00Z", "2026-04-08T17:35:00Z", actualWorkMinutes=455)],
    )
    assert _day_actual_minutes(day, TZ, now=NOW) == 455


def test_leave_time_office_then_remote_anchors_on_open_session():
    """Bug 2: departure is measured from the remote clock-in, crediting office time."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),  # 0 overtime → required_today = 480
        _make_office_then_remote_day(
            "2026-04-08",
            office_in="2026-04-08T09:00:00Z",
            office_out="2026-04-08T10:00:00Z",
            office_minutes=60,  # 1h banked at the office
            remote_in="2026-04-08T13:00:00Z",
        ),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # remaining = 480 - 60 = 420 (>6h) → break; leave = 13:00 + 420 + 60 = 21:00.
    # (The buggy day-level path gave 09:00 + 480 + 60 = 18:00.)
    assert leave_times == [LeaveTime(includes_break=True, min_time=Duration.parse("21:00"))]


def test_leave_time_office_then_remote_two_windows():
    """Office credit can drop the remote remainder into the 5–6h two-window range."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=570),  # +90 → required_today = 390
        _make_office_then_remote_day(
            "2026-04-08",
            office_in="2026-04-08T09:00:00Z",
            office_out="2026-04-08T10:00:00Z",
            office_minutes=60,
            remote_in="2026-04-08T13:00:00Z",
        ),
    ]
    # remaining = 390 - 60 = 330 (between 300 and 360) → two windows from 13:00.
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    assert leave_times == [
        LeaveTime(includes_break=False, min_time=Duration.parse("18:30"), max_time=Duration.parse("19:00")),
        LeaveTime(includes_break=True, min_time=Duration.parse("19:30")),
    ]


def test_leave_time_office_credit_can_cover_target():
    """When the closed office session already meets today's target, the remainder is
    zero, so the leave time is just the remote clock-in (no special case, no break)."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=600),  # +120 → required_today = 360
        _make_office_then_remote_day(
            "2026-04-08",
            office_in="2026-04-08T08:00:00Z",
            office_out="2026-04-08T15:00:00Z",
            office_minutes=360,  # already banked the full 6h target
            remote_in="2026-04-08T18:00:00Z",
        ),
    ]
    # remaining = 360 - 360 = 0 → leave = remote clock-in (18:00), no break.
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    assert leave_times == [LeaveTime(includes_break=False, min_time=Duration.parse("18:00"))]


def test_latest_clock_in_time_uses_current_session():
    """balance's 'Last day' clock-in is the latest session's start, not the morning
    on-site one."""
    day = _make_office_then_remote_day(
        "2026-04-08",
        office_in="2026-04-08T09:00:00Z",
        office_out="2026-04-08T12:00:00Z",
        office_minutes=180,
        remote_in="2026-04-08T13:00:00Z",
    )
    assert get_latest_clock_in_time(day, TZ) == Duration.parse("13:00")
