from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from praiselul.config import Config
from praiselul.duration import Duration
from praiselul.errors import NoClockInError

_MIN_HOURS_FOR_MANDATORY_BREAK = Duration(6 * 60)
_MANDATORY_BREAK = Duration(60)

# Japanese weekday abbreviations (Mon=0 .. Sun=6)
_WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def day_label(date_str: str) -> str:
    """Convert YYYY-MM-DD to 'M/D(曜)' format like RecoLul."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    weekday = _WEEKDAYS_JA[dt.weekday()]
    return f"{dt.month}/{dt.day}({weekday})"


def until_today(days: list[dict[str, Any]], tz: ZoneInfo | None = None) -> list[dict[str, Any]]:
    """Return only days up to and including today."""
    if tz:
        today = datetime.now(tz).strftime("%Y-%m-%d")
    else:
        today = datetime.now().strftime("%Y-%m-%d")
    return [d for d in days if d["date"] <= today]


_NON_WORKING_DAY_TYPES = {"scheduled_rest_day", "statutory_rest_day", "holiday"}

# Paid-leave units that cover half of a scheduled day.
_HALF_DAY_LEAVE_UNITS = {"half_day_am", "half_day_pm"}


def _is_working_day(day: dict[str, Any]) -> bool:
    return day.get("dayType") not in _NON_WORKING_DAY_TYPES


def _day_expected_minutes(day: dict[str, Any], config: Config) -> int:
    """Expected minutes for overtime calculation.

    Uses config.hours_per_day for working days (matching RecoLul's behavior)
    and 0 for non-working days (rest days, holidays).

    Paid leave (any category other than unpaid) credits the day's scheduled
    hours, so it must not register as a shortfall: a full paid-leave day expects
    0 and a half day expects half. Any other leave unit defers to the
    timesheet's reported expected hours. Unpaid leave still owes the full hours,
    so it is left untouched.
    """
    if not _is_working_day(day):
        return 0
    base = config.hours_per_day * 60
    leave_category = day.get("leaveCategory")
    if leave_category and leave_category != "unpaid":
        unit = day.get("leaveUnit")
        if unit == "full_day":
            return 0
        if unit in _HALF_DAY_LEAVE_UNITS:
            return base // 2
        # Other leave units may not cover half a day, so defer to the
        # timesheet's own leave-adjusted value instead of assuming.
        return int(day.get("expectedMinutes", base))
    return base


def _has_activity(day: dict[str, Any], config: Config) -> bool:
    """Whether this day has work or expected work."""
    actual = day.get("actualWorkMinutes") or 0
    expected = _day_expected_minutes(day, config)
    return actual > 0 or expected > 0


def _session_recorded_break_minutes(session: dict[str, Any]) -> int:
    """Minutes of break already recorded against a session by Praise.

    Closed sessions carry a computed ``breakMinutes``; the open session leaves it
    null but still reports its ``breakPeriods`` (recorded break_start/break_end
    pairs), so fall back to summing those.
    """
    recorded = session.get("breakMinutes")
    if recorded is not None:
        return int(recorded)
    return sum(int(bp.get("minutes") or 0) for bp in session.get("breakPeriods") or [])


def _open_session_work_minutes(session: dict[str, Any], now: datetime, tz: ZoneInfo) -> int:
    """Net minutes worked in the in-progress session, measured up to ``now``.

    Mirrors Praise's per-session ``punch_priority`` break handling (which itself
    matches RecoLul): a break already recorded for the session is subtracted and
    suppresses the auto-break; otherwise the mandatory 1h break is deducted only
    once this single session's gross reaches the threshold.
    """
    clock_in = _parse_iso_to_local(session.get("clockIn"), tz)
    if not clock_in:
        return 0
    gross = max(0, int((now - clock_in).total_seconds() / 60))

    recorded_break = _session_recorded_break_minutes(session)
    if recorded_break > 0:
        deduction = recorded_break
    elif gross >= _MIN_HOURS_FOR_MANDATORY_BREAK.minutes:
        deduction = _MANDATORY_BREAK.minutes
    else:
        deduction = 0
    return max(0, gross - deduction)


def _current_day_worked_minutes(day: dict[str, Any], now: datetime, tz: ZoneInfo) -> int:
    """Live net work minutes for a day that still has an open session.

    Sums per session: closed sessions keep Praise's already-break-adjusted
    ``actualWorkMinutes``, and the open session is measured up to ``now`` with
    the mandatory break applied only when no break is already recorded for it.
    Because each session is handled independently, clocking in/out/in (a real
    break) never triggers an extra auto-break, and inter-session gaps are not
    counted as work.
    """
    total = 0
    for session in day.get("sessions") or []:
        if session.get("clockOut"):
            actual = session.get("actualWorkMinutes")
            if actual is not None:
                total += int(actual)
            else:
                # Closed session without a precomputed value — derive it.
                clock_in = _parse_iso_to_local(session.get("clockIn"), tz)
                clock_out = _parse_iso_to_local(session.get("clockOut"), tz)
                if clock_in and clock_out:
                    gross = max(0, int((clock_out - clock_in).total_seconds() / 60))
                    total += max(0, gross - _session_recorded_break_minutes(session))
        else:
            total += _open_session_work_minutes(session, now, tz)
    return total


def _day_actual_minutes(day: dict[str, Any], tz: ZoneInfo) -> int:
    """Get actual work minutes for a day, computing live value for open sessions."""
    actual = day.get("actualWorkMinutes")
    if actual is not None:
        return int(actual)
    # Open session — compute live, applying the same per-session break logic Praise
    # uses for closed days.
    if _is_open_session(day):
        return _current_day_worked_minutes(day, datetime.now(tz), tz)
    return 0


def get_overtime_history(
    days: list[dict[str, Any]], config: Config, tz: ZoneInfo,
) -> tuple[list[str], list[Duration]]:
    """Compute per-day overtime from Praise daily records.

    Returns (day_labels, daily_overtime_list).
    """
    labels = []
    overtime_history = []
    for day in days:
        actual = _day_actual_minutes(day, tz)
        expected = _day_expected_minutes(day, config)

        if not (actual or expected):
            continue

        labels.append(day_label(day["date"]))
        overtime_history.append(Duration(actual - expected))

    return labels, overtime_history


def get_overtime_balance(days: list[dict[str, Any]], config: Config, tz: ZoneInfo) -> Duration:
    """Sum of daily overtime across all provided days."""
    _, history = get_overtime_history(days, config, tz)
    return sum(history, Duration())


def get_workplace_times(summary: dict[str, Any]) -> dict[str, Duration]:
    """Extract workplace breakdown from the timesheet summary."""
    result = {}
    on_site = summary.get("onSiteMinutes", 0)
    remote = summary.get("remoteMinutes", 0)
    if on_site:
        result["On-site"] = Duration.from_minutes(on_site)
    if remote:
        result["Remote"] = Duration.from_minutes(remote)
    return result


def get_today_record(days: list[dict[str, Any]], tz: ZoneInfo) -> dict[str, Any] | None:
    """Get today's daily record if it exists."""
    today = datetime.now(tz).strftime("%Y-%m-%d")
    for day in days:
        if day["date"] == today:
            return day
    return None


def _parse_iso_to_local(iso_str: str | None, tz: ZoneInfo) -> datetime | None:
    """Parse an ISO datetime string and convert to the given timezone."""
    if not iso_str:
        return None
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def get_clock_in_time(day: dict[str, Any], tz: ZoneInfo) -> Duration | None:
    """Extract clock-in time as a Duration (local time) from a daily record."""
    dt = _parse_iso_to_local(day.get("clockIn"), tz)
    if not dt:
        return None
    return Duration(dt.hour * 60 + dt.minute)


@dataclasses.dataclass
class LeaveTime:
    includes_break: bool
    min_time: Duration
    max_time: Duration | None = None


def get_leave_time(days: list[dict[str, Any]], config: Config, tz: ZoneInfo) -> list[LeaveTime]:
    """Calculate when to leave to reach zero monthly overtime.

    Uses the same 3-scenario break logic as RecoLul.
    """
    today_days = until_today(days, tz)
    if not today_days:
        raise NoClockInError()

    today = today_days[-1]
    clock_in = get_clock_in_time(today, tz)

    # If no clock-in or already clocked out, raise
    if not clock_in:
        raise NoClockInError()
    if today.get("clockOut") and not _is_open_session(today):
        raise NoClockInError()

    # Compute overtime balance from all days before today
    previous_days = today_days[:-1]
    overtime_balance = get_overtime_balance(previous_days, config, tz)

    # Leave-adjusted target: a half-day leave today only requires the other half.
    day_base_hours = Duration(_day_expected_minutes(today, config))
    required_today = day_base_hours - overtime_balance

    leave_time_without_break = clock_in + required_today
    leave_time_with_break = clock_in + required_today + Duration(60)

    if required_today > _MIN_HOURS_FOR_MANDATORY_BREAK:
        return [
            LeaveTime(
                includes_break=True,
                min_time=leave_time_with_break,
            )
        ]
    if required_today > _MIN_HOURS_FOR_MANDATORY_BREAK - Duration(60):
        # Between 5 and 6 hours: two windows
        first_leave_time = LeaveTime(
            includes_break=False,
            min_time=leave_time_without_break,
            max_time=clock_in + _MIN_HOURS_FOR_MANDATORY_BREAK,
        )
        second_leave_time = LeaveTime(
            includes_break=True,
            min_time=leave_time_with_break,
        )
        return [first_leave_time, second_leave_time]
    return [
        LeaveTime(
            includes_break=False,
            min_time=leave_time_without_break,
        )
    ]


def _is_open_session(day: dict[str, Any]) -> bool:
    """Check if the day has an open (not yet clocked out) session."""
    sessions = day.get("sessions", [])
    return any(s.get("clockOut") is None for s in sessions)
