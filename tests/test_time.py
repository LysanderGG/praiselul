from datetime import datetime
from zoneinfo import ZoneInfo

from praiselul.config import Config
from praiselul.duration import Duration
from praiselul.time import (
    LeaveTime,
    _current_day_worked_minutes,
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
