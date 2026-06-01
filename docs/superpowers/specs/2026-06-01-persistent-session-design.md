# Persistent session for praiselul

**Date:** 2026-06-01
**Status:** Approved

## Problem

Every CLI command opens a fresh `PraiseSession`. On `__enter__` it makes two
network round-trips — `_fetch_build_version()` (GET `/api/health`) and
`_login()` (POST `/api/auth/login`) — then discards the `requests.Session` and
its auth cookie on exit. The next command logs in again from scratch.

The acute pain is **audit/security noise**: each login produces a server-side
audit event. The goal is to **minimize the number of `/api/auth/login` events**,
not merely to speed commands up.

## Goal

Reuse a stored session for as long as the server accepts it. Only call
`_login()` when the cookie is missing or actively rejected.

## Design

### Storage

- Persist the `requests` cookie jar to `~/.praiselul/session` (same directory as
  `config.ini`), written with `0600` permissions.
- Use `http.cookiejar.LWPCookieJar` with `ignore_discard=True` so a server
  session/discard cookie is persisted across invocations. Plain-text format —
  **no pickle** (avoids arbitrary-code-execution on unpickling a tampered file).
- Cache the `X-Build-Version` string alongside the session (e.g. a small
  `session.meta` / paired value) so a warm run also skips the `/api/health`
  preflight. Refresh it whenever we do a cold login.

The trust level is unchanged: the password already lives in plaintext in
`config.ini`, so a plaintext cookie file in the same directory is no worse.

### Flow on `__enter__`

- Load cookies + build version from disk.
  - **Present** → assume valid. Do **not** login, do **not** hit `/api/health`.
  - **Absent / corrupt** → treat as no session: `_fetch_build_version()` +
    `_login()`, then persist cookies + build version.

### Lazy re-login on rejection

Wrap the authenticated API calls (`get_timesheet`, `get_clock_status`):

- If the response is `401` (or a `success: false` auth error), perform exactly
  **one** `_login()`, persist the refreshed cookies, and retry the call once.
- This is the only path that creates a login event after the first, so login
  frequency drops to roughly once per cookie lifetime.

### Error handling

- A corrupt or unreadable session file is treated as "no session" (ignored /
  overwritten), never a hard crash.
- `InvalidPraiseLoginError` still propagates if the re-login itself fails (e.g.
  bad password).

## Alternatives considered

- **Validate the session on every startup with a cheap call** — rejected: adds a
  guaranteed round-trip and does not reduce login count; lazy-on-401 already
  handles invalidation with no preflight.
- **Pickle the `RequestsCookieJar`** — rejected: introduces unpickle RCE risk for
  a file we can instead store as readable text via `LWPCookieJar`.

## Testing

Unit tests with a mocked `requests.Session`:

1. Warm start (valid session file present) performs no login and no `/api/health`
   call.
2. Cold start (no session file) logs in and writes the session file.
3. A `401` on an authenticated call triggers exactly one re-login, one retry, and
   re-persists the cookies.
4. A corrupt session file falls back to a fresh login without crashing.
