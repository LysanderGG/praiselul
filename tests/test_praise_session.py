from http.cookiejar import LWPCookieJar
from unittest import mock

import requests

from praiselul.praise.praise_session import PraiseSession


def _resp(status_code: int, json_data: dict):
    response = mock.MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status.return_value = None
    return response


def _session_mock() -> mock.MagicMock:
    m = mock.MagicMock()
    m.cookies = requests.cookies.RequestsCookieJar()
    m.headers = {}
    return m


def _write_session_file(session_path, build_version: str = "1.2.3"):
    jar = LWPCookieJar(str(session_path))
    jar.set_cookie(requests.cookies.create_cookie(name="praise_session", value="abc", domain="praise.test"))
    jar.save(ignore_discard=True)
    session_path.with_suffix(".meta").write_text(build_version)


def _make_session(tmp_path, mock_session) -> PraiseSession:
    return PraiseSession(
        base_url="https://praise.test",
        email="me@praise.test",
        password="secret",
        session_path=str(tmp_path / "session"),
    )


def test_warm_start_skips_login_and_health(tmp_path):
    """A valid session file on disk means no login and no /api/health call."""
    _write_session_file(tmp_path / "session")
    m = _session_mock()

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path, m):
            pass

    m.post.assert_not_called()
    m.get.assert_not_called()
    assert m.headers["X-Build-Version"] == "1.2.3"


def test_cold_start_logs_in_and_writes_session_file(tmp_path):
    """With no session file, the first run fetches the build version, logs in, and persists."""
    session_path = tmp_path / "session"
    m = _session_mock()
    m.get.return_value = _resp(200, {"version": "9.9.9"})

    def _login(*args, **kwargs):
        m.cookies.set("praise_session", "fresh", domain="praise.test", path="/")
        return _resp(200, {"success": True})

    m.post.side_effect = _login

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path, m):
            pass

    assert m.get.call_args[0][0] == "https://praise.test/api/health"
    assert m.post.call_count == 1
    assert m.post.call_args[0][0] == "https://praise.test/api/auth/login"
    assert session_path.is_file()
    assert session_path.with_suffix(".meta").read_text().strip() == "9.9.9"


def test_401_triggers_single_relogin_and_retry(tmp_path):
    """An expired cookie (401) re-logs in exactly once, retries, and re-persists the new cookie."""
    session_path = tmp_path / "session"
    _write_session_file(session_path)
    m = _session_mock()
    m.get.side_effect = [
        _resp(401, {}),
        _resp(200, {"success": True, "data": {"days": []}}),
    ]

    def _login(*args, **kwargs):
        m.cookies.set("praise_session", "renewed", domain="praise.test", path="/")
        return _resp(200, {"success": True})

    m.post.side_effect = _login

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path, m) as session:
            data = session.get_timesheet(year=2026, month=6)

    assert data == {"days": []}
    assert m.post.call_count == 1
    assert m.get.call_count == 2
    # The renewed cookie was persisted back to disk.
    assert "renewed" in session_path.read_text()


def test_426_refreshes_build_version_and_retries(tmp_path):
    """A stale cached build version (426) re-fetches the version, retries, and re-persists it."""
    session_path = tmp_path / "session"
    _write_session_file(session_path, build_version="1.2.3")
    m = _session_mock()
    m.get.side_effect = [
        _resp(426, {}),
        _resp(200, {"version": "2.0.0"}),  # /api/health refresh
        _resp(200, {"success": True, "data": {"days": []}}),
    ]

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path, m) as session:
            data = session.get_timesheet(year=2026, month=6)

    assert data == {"days": []}
    m.post.assert_not_called()
    assert m.get.call_count == 3
    assert m.get.call_args_list[1][0][0] == "https://praise.test/api/health"
    assert m.headers["X-Build-Version"] == "2.0.0"
    # The refreshed build version was persisted back to disk.
    assert session_path.with_suffix(".meta").read_text().strip() == "2.0.0"


def test_corrupt_session_file_falls_back_to_fresh_login(tmp_path):
    """A garbage session file is treated as no session, not a crash."""
    session_path = tmp_path / "session"
    session_path.write_text("this is not a cookie jar")
    m = _session_mock()
    m.get.return_value = _resp(200, {"version": "1.0.0"})
    m.post.return_value = _resp(200, {"success": True})

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path, m):
            pass

    assert m.post.call_count == 1
    assert m.post.call_args[0][0] == "https://praise.test/api/auth/login"
