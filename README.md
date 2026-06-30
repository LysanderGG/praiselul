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

Prompts for the Praise server URL and hours per day.

## Login

Praise authenticates the CLI with a browser-approved device flow (it no longer
accepts a password — login is protected by reCAPTCHA). The first time you run a
command, praiselul prints a short code and opens the approval page:

```
  Opening https://praise.pafin.com/cli/authorize in your browser…
  Enter this code to authorize: ZMND-H966

  Waiting for approval… (Ctrl-C to cancel)
✓ Logged in.
```

Approve it in the browser and the command continues. The token is cached in
`~/.praiselul/token` (revoke it anytime from **Account → Sessions**), so later
commands don't prompt again. Manage CLI sessions there like any other login.

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
- `PRAISE_HOURS_PER_DAY` (default: 8)
- `PRAISE_TOKEN` — a `prs_cli_…` token to use directly, skipping the browser flow (for headless/CI use)
