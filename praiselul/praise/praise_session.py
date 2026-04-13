from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from praiselul.errors import InvalidPraiseLoginError


class PraiseSession:
    def __init__(self, base_url: str, email: str, password: str):
        if not base_url.startswith(("http://", "https://")):
            base_url = f"https://{base_url}"
        self._base_url: str = base_url.rstrip("/")
        self._email: str = email
        self._password: str = password
        self._session: requests.Session | None = None

    def __enter__(self):
        self._session = requests.Session()
        self._fetch_build_version()
        self._login()
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
        response = self.session.get(
            f"{self._base_url}/api/time/my-timesheet",
            params={"year": year, "month": month, "locale": "en"},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"API error: {data.get('error', {}).get('code', 'unknown')}")
        return data["data"]

    def get_clock_status(self) -> dict[str, Any]:
        response = self.session.get(
            f"{self._base_url}/api/time/clock/status",
            params={"locale": "en"},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"API error: {data.get('error', {}).get('code', 'unknown')}")
        return data["data"]

    def _fetch_build_version(self):
        """Fetch the server's build version from /api/health (no version check on that route)
        and set it as a default header for all subsequent requests."""
        response = self.session.get(f"{self._base_url}/api/health")
        response.raise_for_status()
        version = response.json().get("version")
        if version:
            self.session.headers["X-Build-Version"] = version

    def _login(self):
        response = self.session.post(
            f"{self._base_url}/api/auth/login",
            json={"email": self._email, "password": self._password},
        )
        if response.status_code == 401:
            raise InvalidPraiseLoginError()
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise InvalidPraiseLoginError()
