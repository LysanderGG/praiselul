# praiselul

Overtime management CLI for [Praise](https://github.com/Cryptact/praise).

Mirrors [RecoLul](https://github.com/Maerig/recolul)'s interface, powered by Praise's REST API.

## Install

```bash
pip install praiselul
```

## Setup

```bash
praiselul config
```

Prompts for Praise server URL, email, password, and hours per day.

## Commands

```bash
praiselul balance                # Monthly overtime balance + workplace breakdown
praiselul balance --exclude-last-day
praiselul when                   # When to leave to avoid overtime
praiselul graph                  # Plotly overtime progression chart
praiselul graph --exclude-last-day
```

## Config

Stored in `~/.praiselul/config.ini`. Also supports environment variables:

- `PRAISE_URL`
- `PRAISE_EMAIL`
- `PRAISE_PASSWORD`
- `PRAISE_HOURS_PER_DAY` (default: 8)
