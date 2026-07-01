import argparse
import sys
from typing import Any
from zoneinfo import ZoneInfo

from praiselul import __version__, plotting, time
from praiselul.config import Config, DEFAULT_HOURS_PER_DAY
from praiselul.duration import Duration
from praiselul.errors import NoClockInError
from praiselul.praise.praise_session import PraiseSession


def _get_tz(timesheet: dict[str, Any]) -> ZoneInfo:
    return ZoneInfo(timesheet.get("timezone", "UTC"))


def balance(config: Config, exclude_last_day: bool) -> None:
    timesheet = _get_timesheet(config)
    tz = _get_tz(timesheet)
    days = time.until_today(timesheet["days"], tz)
    if exclude_last_day and len(days) > 1:
        days = days[:-1]

    overtime_balance = time.get_overtime_balance(days, config, tz)
    print(f"Monthly overtime balance: {overtime_balance}")

    workplace_times = time.get_workplace_times(timesheet["summary"])
    if workplace_times:
        print("Total time per workplace:")
        for workplace, total_work_time in workplace_times.items():
            print(f"  {workplace}: {total_work_time}")

    summary = timesheet["summary"]
    if summary.get("hasRemoteAllowance"):
        remote_budget = Duration.from_minutes(summary.get("requiredMinutes", 0) - summary.get("requiredOnSiteMinutes", 0))
        print(f"Maximum WFH time this month: {remote_budget}")

    if exclude_last_day:
        return

    today = time.get_today_record(days, tz)
    if today:
        label = time.day_label(today["date"])
        print(f"\nLast day {label}")
        clock_in_time = time.get_latest_clock_in_time(today, tz)
        if clock_in_time:
            print(f"  Clock-in: {clock_in_time}")
        actual_mins = time._day_actual_minutes(today, tz)
        print(f"  Working hours: {Duration.from_minutes(actual_mins)}")
        break_mins = Duration.from_minutes(today.get("breakMinutes"))
        if break_mins:
            print(f"  Break: {break_mins}")


def when_to_leave(config: Config) -> None:
    try:
        timesheet = _get_timesheet(config)
        tz = _get_tz(timesheet)
        leave_times = time.get_leave_time(timesheet["days"], config, tz)
    except NoClockInError:
        print("You have already clocked out.")
        return

    if len(leave_times) == 1:
        break_msg = "(break time included)" if leave_times[0].includes_break else "(break time not included)"
        print(f"Leave at {leave_times[0].min_time} to avoid overtime {break_msg}.")
    else:
        print(
            f"Leave between {leave_times[0].min_time} and {leave_times[0].max_time}, or "
            f"after {leave_times[1].min_time}."
        )


def update_config() -> None:
    praise_url = input("praise.url: ")
    hours_per_day = input(f"praise.hoursPerDay [{DEFAULT_HOURS_PER_DAY}]: ") or str(DEFAULT_HOURS_PER_DAY)
    config = Config(
        praise_url=praise_url,
        hours_per_day=int(hours_per_day),
    )
    config.save()


def graph(config: Config, exclude_last_day: bool) -> None:
    timesheet = _get_timesheet(config)
    tz = _get_tz(timesheet)
    days = time.until_today(timesheet["days"], tz)
    if exclude_last_day and len(days) > 1:
        days = days[:-1]
    labels, history = time.get_overtime_history(days, config, tz)
    plotting.plot_overtime_balance_history(labels, history)


def main() -> None:
    parser = argparse.ArgumentParser(prog="praiselul")
    parser.add_argument("-v", "--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    balance_parser = subparsers.add_parser("balance", help="Calculate overtime balance")
    balance_parser.add_argument(
        "--exclude-last-day",
        action="store_true",
        help="Exclude last/current day from the calculation",
    )

    subparsers.add_parser("when", help="Calculate at which time to leave to avoid overtime this month")

    subparsers.add_parser("config", help="Init or update config")

    graph_parser = subparsers.add_parser("graph", help="Display a graph of overtime balance over the month")
    graph_parser.add_argument(
        "--exclude-last-day",
        action="store_true",
        help="Exclude last/current day from the graph",
    )

    args = parser.parse_args(sys.argv[1:])
    if args.command == "config":
        update_config()
        return

    config = Config.from_env() or Config.load()
    if not config:
        raise RuntimeError("No config found. Run: praiselul config")

    match args.command:
        case "balance":
            balance(config, exclude_last_day=args.exclude_last_day)
        case "when":
            when_to_leave(config)
        case "graph":
            graph(config, exclude_last_day=args.exclude_last_day)


def _get_timesheet(config: Config) -> dict[str, Any]:
    with PraiseSession(base_url=config.praise_url) as session:
        return session.get_timesheet()


if __name__ == "__main__":
    main()
