from datetime import datetime
from zoneinfo import ZoneInfo

from praiselul.config import Config
from praiselul.duration import Duration
from praiselul.time import (
    LeaveTime,
    _closed_day_worked_minutes,
    _current_day_worked_minutes,
    _day_actual_minutes,
    get_latest_clock_in_time,
    get_leave_time,
    get_overtime_balance,
    get_overtime_history,
    get_workplace_times,
)

DEFAULT_CONFIG = Config(praise_url="")  # 8h/day
PART_TIME_CONFIG = Config(praise_url="", hours_per_day=6)

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
    import pytest

    from praiselul.errors import NoClockInError

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


def test_current_day_single_session_at_6h_boundary_no_auto_break():
    """Exactly 6h00 does NOT trip the threshold — Praise applies the break only
    when gross *strictly exceeds* 6h, so neither do we."""
    # 10:00 → 16:00 = 6h = 360 exactly → no deduction
    day = _make_open_day("2026-04-08", clock_in="2026-04-08T10:00:00Z")
    assert _current_day_worked_minutes(day, NOW, TZ) == 360


def test_current_day_single_session_just_over_6h_applies_auto_break():
    """6h01 is over the threshold → the mandatory hour comes off."""
    # 09:59 → 16:00 = 361 → 361 - 60 = 301
    day = _make_open_day("2026-04-08", clock_in="2026-04-08T09:59:00Z")
    assert _current_day_worked_minutes(day, NOW, TZ) == 301


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


def test_current_day_closed_session_deducts_recorded_break():
    """A closed session's recorded break is deducted from its gross (punch
    priority) — same 150 the backend reports as actualWorkMinutes."""
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=None,
        clock_in="2026-04-08T09:00:00Z",
        sessions=[
            # 3h gross minus a recorded 30-min break → 150.
            _session("2026-04-08T09:00:00Z", "2026-04-08T12:00:00Z", actualWorkMinutes=150, breakMinutes=30),
            _session("2026-04-08T13:00:00Z", None),
        ],
    )
    # 150 (closed) + 180 (open 13:00–16:00, <6h) = 330
    assert _current_day_worked_minutes(day, NOW, TZ) == 330


def test_current_day_closed_breakless_session_over_6h_loses_auto_break():
    """Regression (2026-07-22): Praise's session-level actualWorkMinutes hides
    the auto-break, so a closed breakless 6h+ session banks its gross minus the
    mandatory hour — not the face value the session payload claims."""
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=None,
        clock_in="2026-04-08T08:00:00Z",
        sessions=[
            # grossMinutes present as in real Praise payloads; actualWorkMinutes
            # equals gross because no break was punched.
            _session(
                "2026-04-08T08:00:00Z",
                "2026-04-08T15:13:00Z",
                grossMinutes=433,
                actualWorkMinutes=433,
                breakMinutes=0,
            ),
            _session("2026-04-08T15:30:00Z", None),
        ],
    )
    # (433 - 60) + 30 (open 15:30–16:00) = 403 — NOT 433 + 30 = 463.
    assert _current_day_worked_minutes(day, NOW, TZ) == 403


def test_current_day_closed_session_at_exactly_6h_keeps_full_time():
    """A closed breakless session of exactly 6h00 owes no break."""
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=None,
        clock_in="2026-04-08T09:00:00Z",
        sessions=[
            _session("2026-04-08T09:00:00Z", "2026-04-08T15:00:00Z", actualWorkMinutes=360),
            _session("2026-04-08T15:30:00Z", None),
        ],
    )
    # 360 (closed, no deduction at the boundary) + 30 (open) = 390
    assert _current_day_worked_minutes(day, NOW, TZ) == 390


def test_current_day_closed_session_prefers_gross_minutes_over_punches():
    """``grossMinutes`` is the authoritative gross for a closed session; the
    clock-in/out punches are only a fallback. Pinned with a payload where the two
    disagree so the precedence can't silently flip."""
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=None,
        clock_in="2026-04-08T08:00:00Z",
        sessions=[
            # Punches span 433 min, but the reported gross is 400.
            _session(
                "2026-04-08T08:00:00Z",
                "2026-04-08T15:13:00Z",
                grossMinutes=400,
                breakMinutes=0,
            ),
            _session("2026-04-08T15:30:00Z", None),
        ],
    )
    # (400 - 60) + 30 (open 15:30–16:00) = 370 — the punch-derived 433 would give 403.
    assert _current_day_worked_minutes(day, NOW, TZ) == 370


def test_current_day_closed_session_falls_back_to_punches_without_gross_minutes():
    """With no ``grossMinutes`` on the session, the clock-in/out span carries the
    gross instead."""
    day = _make_day(
        "2026-04-08",
        actual_work_minutes=None,
        clock_in="2026-04-08T08:00:00Z",
        sessions=[
            _session("2026-04-08T08:00:00Z", "2026-04-08T15:13:00Z", breakMinutes=0),
            _session("2026-04-08T15:30:00Z", None),
        ],
    )
    # 08:00→15:13 = 433 → (433 - 60) + 30 = 403.
    assert _current_day_worked_minutes(day, NOW, TZ) == 403


def test_closed_day_two_long_sessions_each_lose_their_own_break():
    """The break threshold is resolved per session, not per day: two breakless
    6h01 sessions each owe their own hour. An inter-session gap already satisfies
    the break obligation for the stint before it, so the day is not capped at one.
    """
    sessions = [
        _session(
            "2026-04-08T08:00:00Z",
            "2026-04-08T14:01:00Z",
            grossMinutes=361,
            actualWorkMinutes=361,
            breakMinutes=0,
        ),
        _session(
            "2026-04-08T15:00:00Z",
            "2026-04-08T21:01:00Z",
            grossMinutes=361,
            actualWorkMinutes=361,
            breakMinutes=0,
        ),
    ]
    # (361 - 60) * 2 = 602 — a day-level rule would deduct one hour and give 662.
    assert _closed_day_worked_minutes(_make_day("2026-04-08", sessions=sessions), TZ) == 602
    # The fixture below *documents* — it cannot verify — that Praise reports the same
    # 602 at the day level once the day is fully closed, which is why the live figure
    # doesn't jump at the final clock-out. With no open session `_day_actual_minutes`
    # returns the day-level value verbatim, so all this asserts is that pass-through;
    # the 602 itself rests on the backend's per-session summation, not on this test.
    fully_closed = _make_day("2026-04-08", actual_work_minutes=602, sessions=sessions)
    assert _day_actual_minutes(fully_closed, TZ, NOW) == 602


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


def test_leave_time_open_session_recorded_break_pushes_departure():
    """A break punched inside the open session suppresses the auto-break
    scenarios (punch priority) but pushes departure back by its own length."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),  # 0 balance → required_today = 480
        _make_day(
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
        ),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # Session must gross 480 + the 30 recorded → leave 09:00 + 8:30 = 17:30.
    # Ignoring the recorded break would add the auto hour instead → 18:00.
    assert leave_times == [LeaveTime(includes_break=True, min_time=Duration.parse("17:30"))]


def test_leave_time_short_day_recorded_break_still_counts():
    """Even when the remainder is under 6h (no auto-break in sight), a recorded
    break must extend the stay — otherwise the day ends short by its length."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=680),  # +200 → required_today = 280
        _make_day(
            "2026-04-08",
            actual_work_minutes=None,
            clock_in="2026-04-08T09:00:00Z",
            sessions=[
                _session(
                    "2026-04-08T09:00:00Z",
                    None,
                    breakPeriods=[
                        {"start": "2026-04-08T11:00:00Z", "end": "2026-04-08T11:45:00Z", "minutes": 45}
                    ],
                ),
            ],
        ),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # 09:00 + 280min + 45min break = 14:25 (leaving at the naive 13:40 would
    # bank only 235min once Praise deducts the recorded break).
    assert leave_times == [LeaveTime(includes_break=True, min_time=Duration.parse("14:25"))]


def test_leave_time_credits_closed_session_net_of_pending_auto_break():
    """Regression (2026-07-22): `when` banked a closed breakless 6h+ session at
    face value and suggested leaving an hour early — Praise deducted the
    session's auto-break at final clock-out."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),  # 0 balance → required_today = 480
        _make_day(
            "2026-04-08",
            actual_work_minutes=433,  # day-level mirror of the closed session
            clock_in="2026-04-08T08:00:00Z",
            sessions=[
                _session(
                    "2026-04-08T08:00:00Z",
                    "2026-04-08T15:13:00Z",
                    grossMinutes=433,
                    actualWorkMinutes=433,
                    breakMinutes=0,
                ),
                _session("2026-04-08T15:30:00Z", None),
            ],
        ),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # banked = 433 - 60 = 373 → remaining = 107 (<5h, no break) → 15:30 + 1:47 = 17:17.
    # The buggy face-value banking gave remaining = 47 → 16:17, one hour early.
    assert leave_times == [LeaveTime(includes_break=False, min_time=Duration.parse("17:17"))]


def test_leave_time_banks_closed_session_from_gross_minutes_not_punches():
    """`when` credits the closed session from its reported ``grossMinutes``, not
    from the clock-in/out span. Same precedence as `_closed_session_work_minutes`,
    asserted here too so a regression can't slip through the leave-time path."""
    days = [
        _make_day("2026-04-07", actual_work_minutes=480),  # 0 balance → required_today = 480
        _make_day(
            "2026-04-08",
            actual_work_minutes=400,  # day-level mirror of the closed session
            clock_in="2026-04-08T08:00:00Z",
            sessions=[
                # Punches span 433 min, but the reported gross is 400.
                _session(
                    "2026-04-08T08:00:00Z",
                    "2026-04-08T15:13:00Z",
                    grossMinutes=400,
                    breakMinutes=0,
                ),
                _session("2026-04-08T15:30:00Z", None),
            ],
        ),
    ]
    leave_times = get_leave_time(days, DEFAULT_CONFIG, TZ)
    # banked = 400 - 60 = 340 → remaining = 140 → 15:30 + 2:20 = 17:50.
    # Falling back to the punch-derived 433 would bank 373 and give 17:17.
    assert leave_times == [LeaveTime(includes_break=False, min_time=Duration.parse("17:50"))]


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
