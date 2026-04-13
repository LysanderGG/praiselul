# praiselul — Design Spec

A Python CLI for overtime management built on top of the Praise HR platform. Mirrors RecoLul's UX (same commands, same output style, same Plotly graph) but fetches data from Praise's REST API instead of scraping RecoRu HTML.

## Commands

### `praiselul config`

Interactive setup. Prompts for:

- `praise.url` — Praise server base URL (e.g., `https://praise.example.com`)
- `praise.email` — user email
- `praise.password` — user password (hidden input via `getpass`)
- `praise.hoursPerDay` — contract hours per day (default: 8)

Saves to `~/.praiselul/config.ini`. Also loadable from environment variables: `PRAISE_URL`, `PRAISE_EMAIL`, `PRAISE_PASSWORD`, `PRAISE_HOURS_PER_DAY`.

### `praiselul balance [--exclude-last-day]`

Displays:

```
Monthly overtime balance: 00:51
Total time per workplace:
  On-site: 45:20
  Remote: 10:30

Last day 4/13(日)
  Clock-in: 09:10
  Working hours: 07:19
  Break: 01:00
```

- **Overtime balance**: sum of `(day.actualWorkMinutes - day.expectedMinutes)` across all days with activity or expected work.
- **Workplace breakdown**: `summary.onSiteMinutes` and `summary.remoteMinutes` from the timesheet response.
- **Last day section**: omitted when `--exclude-last-day` is set. Shows clock-in time, work duration, and break duration from today's daily record.

### `praiselul when`

Calculates departure time to reach zero overtime for the month.

```
Leave at 18:31 to avoid overtime (break time included).
```

Logic:

1. Compute overtime balance from all completed days (excluding today).
2. `required_today = hours_per_day - overtime_balance` (in minutes).
3. Get today's clock-in time from the daily record.
4. Apply break scenarios (same as RecoLul):
   - **Required > 6h**: single leave time WITH 1-hour break.
   - **Required 5-6h**: two windows — leave without break (before 6h threshold), or leave with break (after).
   - **Required < 5h**: single leave time WITHOUT break.
5. `leave_time = clock_in + required_today [+ break_if_applicable]`.

If the user has already clocked out today, prints "You have already clocked out."

### `praiselul graph [--exclude-last-day]`

Opens an interactive Plotly scatter plot in the browser:

- X-axis: day labels (`4/1(火)`, `4/2(水)`, etc.)
- Y-axis: cumulative overtime balance in minutes
- Hover shows `"date HH:MM"` format
- Same styling as RecoLul's graph

## Architecture

```
praiselul/
├── __init__.py              # __version__ = "0.1.0"
├── cli.py                   # argparse entry point: balance, when, graph, config
├── config.py                # Config dataclass + load/save (ini + env vars)
├── duration.py              # Duration class (minutes-based, HH:MM display)
├── errors.py                # NoClockedInError, InvalidLoginError
├── plotting.py              # Plotly overtime balance graph
├── time.py                  # Overtime/departure calculations from Praise data
└── praise/
    ├── __init__.py
    └── praise_session.py    # HTTP client: login + API calls
pyproject.toml               # Build config, entry point, dependencies
tests/
└── test_time.py             # Unit tests for time calculations
```

## Praise API Integration

### Authentication

`PraiseSession` uses `requests.Session` for cookie persistence:

1. `POST {base_url}/api/auth/login` with `{"email": ..., "password": ...}`
2. Response sets `session_id` cookie automatically
3. Subsequent requests include the cookie

### Endpoints Used

| Endpoint | Method | Returns | Used by |
|---|---|---|---|
| `/api/auth/login` | POST | User info + session cookie | All commands |
| `/api/time/my-timesheet?year=Y&month=M` | GET | `TimesheetResponse` | `balance`, `when`, `graph` |
| `/api/time/clock/status` | GET | `ClockStatus` | `when` (to check if clocked in) |

### Data Mapping

Praise's `TimesheetResponse` provides:

- `days[]` — array of `DailyTimeRecord`:
  - `date` (YYYY-MM-DD), `dayType` ("working_day" | "scheduled_rest_day" | "statutory_rest_day" | "holiday")
  - `clockIn`, `clockOut` (ISO datetime or null)
  - `actualWorkMinutes`, `breakMinutes`, `grossMinutes`, `expectedMinutes`
  - `sessions[]` with `locationName`
- `summary` — monthly aggregates:
  - `totalActualWorkMinutes`, `requiredMinutes`
  - `onSiteMinutes`, `remoteMinutes`
  - `workingDaysCount`, `workedDaysCount`

The `expectedMinutes` field on each day represents `standard_hours_per_day` from the work policy (adjusted for leave). This serves the same purpose as RecoLul's `config.hours_per_day` but comes from the policy rather than local config. For the `when` command, we use `config.hours_per_day` as the target (matching RecoLul's behavior), but the balance calculation uses each day's `expectedMinutes` for accuracy.

## Day Label Format

RecoLul shows day labels like `4/7(月)`. We replicate this from the `date` field (YYYY-MM-DD) using Python's locale-aware weekday abbreviation, with Japanese weekday names: 月火水木金土日.

## Config

```ini
[praise]
url = https://praise.example.com
email = user@company.com
password = secret
hoursPerDay = 8
```

**Location**: `~/.praiselul/config.ini`

**Environment variable overrides** (higher priority than config file):

- `PRAISE_URL`
- `PRAISE_EMAIL`
- `PRAISE_PASSWORD`
- `PRAISE_HOURS_PER_DAY`

## Dependencies

```
requests~=2.31
plotly~=5.18
```

## Packaging

```toml
[project]
name = "praiselul"
requires-python = ">=3.10"

[project.scripts]
praiselul = "praiselul.cli:main"
```

Installable via `pip install -e .` for development.

## Differences from RecoLul

| Aspect | RecoLul | praiselul |
|---|---|---|
| Data source | RecoRu HTML scraping | Praise REST API (JSON) |
| Auth | Contract ID + auth ID + password | Email + password (cookie session) |
| WFH tracking | Per-entry workplace from HTML | `summary.onSiteMinutes` / `summary.remoteMinutes` |
| Break handling (when) | Fixed 60-min rule | Fixed 60-min rule (same) |
| Day type detection | Color-based (blue/red) | `dayType` field from API |
| `hours_per_day` source | Config only | Config for `when` target; API `expectedMinutes` for daily balance |
| GUI | PySide6 (separate) | Planned menubar app (separate, later) |
