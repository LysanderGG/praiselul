from __future__ import annotations

import os
from datetime import datetime
from http.cookiejar import LoadError, LWPCookieJar
from typing import Any

import requests

from praiselul.config import DEFAULT_SESSION_PATH
from praiselul.errors import InvalidPraiseLoginError


class PraiseSession:
    def __init__(self, base_url: str, email: str, password: str, session_path: str = DEFAULT_SESSION_PATH):
        if not base_url.startswith(("http://", "https://")):
            base_url = f"https://{base_url}"
        self._base_url: str = base_url.rstrip("/")
        self._email: str = email
        self._password: str = password
        self._session_path: str = session_path
        self._session: requests.Session | None = None

    @property
    def _meta_path(self) -> str:
        """File holding the cached build version, alongside the cookie file."""
        return f"{self._session_path}.meta"

    def __enter__(self):
        self._session = requests.Session()
        if not self._load_session():
            self._fetch_build_version()
            self._login()
            self._save_session()
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
        """GET that transparently re-logs-in once if the stored session was rejected."""
        response = self.session.get(url, **kwargs)
        if response.status_code == 401:
            self._login()
            self._save_session()
            response = self.session.get(url, **kwargs)
        return response

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

    def _load_session(self) -> bool:
        """Load persisted cookies and build version. Returns True if a usable session was restored.

        A missing, empty, or corrupt session file is treated as "no session" so the caller
        falls back to a fresh login rather than crashing.
        """
        if not os.path.isfile(self._session_path):
            return False
        jar = LWPCookieJar(self._session_path)
        try:
            jar.load(ignore_discard=True)
        except (OSError, LoadError):
            return False
        if not len(jar):
            return False
        self.session.cookies.update(jar)

        build_version = self._load_build_version()
        if build_version:
            self.session.headers["X-Build-Version"] = build_version
        else:
            self._fetch_build_version()
        return True

    def _save_session(self):
        """Persist the current cookies (0600) and build version next to the config."""
        os.makedirs(os.path.dirname(self._session_path), exist_ok=True)
        jar = LWPCookieJar(self._session_path)
        for cookie in self.session.cookies:
            jar.set_cookie(cookie)
        jar.save(ignore_discard=True)
        os.chmod(self._session_path, 0o600)
        self._save_build_version()

    def _load_build_version(self) -> str | None:
        try:
            with open(self._meta_path) as meta_file:
                return meta_file.read().strip() or None
        except OSError:
            return None

    def _save_build_version(self):
        version = self.session.headers.get("X-Build-Version")
        if not version:
            return
        with open(self._meta_path, "w") as meta_file:
            meta_file.write(version)
        os.chmod(self._meta_path, 0o600)
