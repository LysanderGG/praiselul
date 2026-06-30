from __future__ import annotations

import os
import re
import socket
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any

import requests

from praiselul import __version__
from praiselul.config import DEFAULT_TOKEN_PATH
from praiselul.errors import CliLoginDeniedError, CliLoginError, CliLoginExpiredError

# Praise gates its password login behind reCAPTCHA, which a headless CLI cannot
# satisfy. The sanctioned path for non-browser clients is the device-authorization
# flow (RFC 8628): the CLI requests a short code, the user approves it in a browser,
# and the CLI receives a long-lived Bearer token. That token then authenticates
# every /api/* call the same way a web session cookie does.

# Safety bound on polling if the server's expiresAt can't be parsed.
DEVICE_FLOW_MAX_WAIT_SECONDS = 300


class PraiseSession:
    def __init__(self, base_url: str, token_path: str = DEFAULT_TOKEN_PATH):
        if not base_url.startswith(("http://", "https://")):
            base_url = f"https://{base_url}"
        self._base_url: str = base_url.rstrip("/")
        self._token_path: str = token_path
        self._token: str | None = None
        # A token from PRAISE_TOKEN is owned by the caller: never persist it and
        # never silently replace it via the device flow.
        self._token_from_env: bool = False
        self._session: requests.Session | None = None

    def __enter__(self):
        self._session = requests.Session()
        # Sent on every request (including the pre-auth device-flow calls) to
        # bypass the SPA build-version check; CLI version freshness is enforced
        # separately server-side.
        self._session.headers["X-Praise-CLI-Version"] = __version__

        env_token = os.environ.get("PRAISE_TOKEN")
        if env_token:
            self._token = env_token
            self._token_from_env = True
        else:
            self._token = self._load_token()

        if self._token:
            self._apply_token()
        else:
            self._authenticate()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._session.close()
        self._session = None

    @property
    def session(self) -> requests.Session:
        assert self._session, "PraiseSession should be used as a context manager"
        return self._session

    def get_timesheet(self, year: int | None = None, month: int | None = None) -> dict[str, Any]:
        now = datetime.now()
        year = year or now.year
        month = month or now.month
        response = self._get(
            f"{self._base_url}/api/time/my-timesheet",
            params={"year": year, "month": month, "locale": "en"},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"API error: {data.get('error', {}).get('code', 'unknown')}")
        return data["data"]

    def get_clock_status(self) -> dict[str, Any]:
        response = self._get(
            f"{self._base_url}/api/time/clock/status",
            params={"locale": "en"},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"API error: {data.get('error', {}).get('code', 'unknown')}")
        return data["data"]

    def _get(self, url: str, **kwargs) -> requests.Response:
        """GET that recovers once from an expired or revoked token (401) by
        re-running the device flow and retrying. A caller-supplied PRAISE_TOKEN
        is never replaced — a 401 there is surfaced for the caller to fix."""
        response = self.session.get(url, **kwargs)
        if response.status_code == 401 and not self._token_from_env:
            self._authenticate()
            response = self.session.get(url, **kwargs)
        return response

    def _apply_token(self):
        self.session.headers["Authorization"] = f"Bearer {self._token}"

    def _authenticate(self):
        """Run the browser-approved device flow, then store and apply the token."""
        start = self._start_device_login()
        print(f"  Opening {start['verificationUrl']} in your browser…")
        print(f"  Enter this code to authorize: {_format_user_code(start['userCode'])}")
        try:
            webbrowser.open(start["verificationUrl"])
        except Exception:
            # Headless environments have no browser; the printed URL still works.
            pass
        print("\n  Waiting for approval… (Ctrl-C to cancel)")

        self._token = self._poll_for_token(start)
        self._apply_token()
        self._save_token(self._token)
        print("✓ Logged in.")

    def _start_device_login(self) -> dict[str, Any]:
        body: dict[str, str] = {}
        label = _device_label()
        if label:
            body["label"] = label
        response = self.session.post(f"{self._base_url}/api/auth/cli/start", json=body)
        response.raise_for_status()
        return response.json()["data"]

    def _poll_for_token(self, start: dict[str, Any]) -> str:
        device_code = start["deviceCode"]
        interval = start.get("intervalSeconds", 5)
        deadline = _poll_deadline(start.get("expiresAt"))
        while time.monotonic() < deadline:
            time.sleep(interval)
            response = self.session.post(
                f"{self._base_url}/api/auth/cli/token",
                json={"deviceCode": device_code},
            )
            if response.status_code == 200:
                return response.json()["data"]["token"]
            code = _error_code(response)
            if code == "apiError.cliLoginPending":
                continue
            if code == "apiError.cliLoginRejected":
                raise CliLoginDeniedError()
            if code in ("apiError.cliLoginExpired", "apiError.cliLoginCodeInvalid"):
                raise CliLoginExpiredError()
            response.raise_for_status()
            raise CliLoginError(f"Unexpected response while authorizing: {response.status_code}")
        raise CliLoginExpiredError()

    def _load_token(self) -> str | None:
        try:
            with open(self._token_path) as token_file:
                token = token_file.read().strip()
        except OSError:
            return None
        return token or None

    def _save_token(self, token: str):
        os.makedirs(os.path.dirname(self._token_path), exist_ok=True)
        with open(self._token_path, "w") as token_file:
            token_file.write(token)
        os.chmod(self._token_path, 0o600)


def _format_user_code(raw: str) -> str:
    """Display the 8-char user code as XXXX-XXXX for readability. The approval
    page accepts it with or without the dash."""
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:]}"
    return raw


def _device_label() -> str | None:
    """A human-friendly device name for the Sessions list, constrained to the
    server's allowed charset."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname())[:100]
    return cleaned or None


def _error_code(response: requests.Response) -> str | None:
    try:
        return response.json().get("error", {}).get("code")
    except ValueError:
        return None


def _poll_deadline(expires_at: str | None) -> float:
    """Translate the server's absolute expiry into a monotonic deadline, falling
    back to a fixed bound if it's missing or unparseable."""
    base = time.monotonic()
    if not expires_at:
        return base + DEVICE_FLOW_MAX_WAIT_SECONDS
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return base + DEVICE_FLOW_MAX_WAIT_SECONDS
    remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    return base + max(0.0, remaining)
