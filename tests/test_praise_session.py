from unittest import mock

import pytest
import requests

from praiselul.errors import CliLoginDeniedError, CliLoginExpiredError
from praiselul.praise.praise_session import PraiseSession

# A user code far in the future so _poll_deadline never short-circuits in tests.
START_DATA = {
    "deviceCode": "d" * 32,
    "userCode": "ZMNDH966",
    "verificationUrl": "https://praise.test/cli/authorize",
    "expiresAt": "2999-01-01T00:00:00Z",
    "intervalSeconds": 5,
}


def _resp(status_code: int, json_data: dict):
    response = mock.MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    else:
        response.raise_for_status.return_value = None
    return response


def _session_mock() -> mock.MagicMock:
    m = mock.MagicMock()
    m.headers = {}
    return m


def _make_session(tmp_path) -> PraiseSession:
    return PraiseSession(base_url="https://praise.test", token_path=str(tmp_path / "token"))


@pytest.fixture(autouse=True)
def _no_browser_no_sleep():
    with (
        mock.patch("praiselul.praise.praise_session.webbrowser.open"),
        mock.patch("praiselul.praise.praise_session.time.sleep"),
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("PRAISE_TOKEN", raising=False)


def test_cold_start_runs_device_flow_and_saves_token(tmp_path):
    """No saved token: start the device flow, poll once, persist the token, set the Bearer header."""
    token_path = tmp_path / "token"
    m = _session_mock()
    m.post.side_effect = [
        _resp(200, {"success": True, "data": START_DATA}),
        _resp(200, {"success": True, "data": {"token": "prs_cli_freshtoken"}}),
    ]

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path):
            pass

    assert m.post.call_args_list[0][0][0] == "https://praise.test/api/auth/cli/start"
    assert m.post.call_args_list[1][0][0] == "https://praise.test/api/auth/cli/token"
    assert m.headers["Authorization"] == "Bearer prs_cli_freshtoken"
    assert m.headers["X-Praise-CLI-Version"]
    assert token_path.read_text() == "prs_cli_freshtoken"


def test_warm_start_uses_saved_token_without_device_flow(tmp_path):
    """A saved token is loaded and applied; no device-flow calls happen."""
    (tmp_path / "token").write_text("prs_cli_saved")
    m = _session_mock()

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path):
            pass

    m.post.assert_not_called()
    assert m.headers["Authorization"] == "Bearer prs_cli_saved"


def test_env_token_is_used_and_not_persisted(tmp_path, monkeypatch):
    """PRAISE_TOKEN wins over any file and is never written to disk."""
    token_path = tmp_path / "token"
    monkeypatch.setenv("PRAISE_TOKEN", "prs_cli_fromenv")
    m = _session_mock()

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path):
            pass

    m.post.assert_not_called()
    assert m.headers["Authorization"] == "Bearer prs_cli_fromenv"
    assert not token_path.exists()


def test_polling_waits_for_pending_then_succeeds(tmp_path):
    """A pending (409) token poll loops until the request is claimed."""
    m = _session_mock()
    m.post.side_effect = [
        _resp(200, {"success": True, "data": START_DATA}),
        _resp(409, {"success": False, "error": {"code": "apiError.cliLoginPending"}}),
        _resp(200, {"success": True, "data": {"token": "prs_cli_after_pending"}}),
    ]

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path):
            pass

    assert m.headers["Authorization"] == "Bearer prs_cli_after_pending"
    assert m.post.call_count == 3


def test_denied_raises(tmp_path):
    """A denied approval surfaces as CliLoginDeniedError."""
    m = _session_mock()
    m.post.side_effect = [
        _resp(200, {"success": True, "data": START_DATA}),
        _resp(403, {"success": False, "error": {"code": "apiError.cliLoginRejected"}}),
    ]

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with pytest.raises(CliLoginDeniedError):
            with _make_session(tmp_path):
                pass


def test_expired_raises(tmp_path):
    """An expired request surfaces as CliLoginExpiredError."""
    m = _session_mock()
    m.post.side_effect = [
        _resp(200, {"success": True, "data": START_DATA}),
        _resp(400, {"success": False, "error": {"code": "apiError.cliLoginExpired"}}),
    ]

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with pytest.raises(CliLoginExpiredError):
            with _make_session(tmp_path):
                pass


def test_401_triggers_reauth_and_retry(tmp_path):
    """An expired/revoked token (401 on a GET) re-runs the device flow, retries, and re-persists."""
    token_path = tmp_path / "token"
    token_path.write_text("prs_cli_stale")
    m = _session_mock()
    m.get.side_effect = [
        _resp(401, {"success": False, "error": {"code": "apiError.sessionExpired"}}),
        _resp(200, {"success": True, "data": {"days": []}}),
    ]
    m.post.side_effect = [
        _resp(200, {"success": True, "data": START_DATA}),
        _resp(200, {"success": True, "data": {"token": "prs_cli_renewed"}}),
    ]

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path) as session:
            data = session.get_timesheet(year=2026, month=6)

    assert data == {"days": []}
    assert m.get.call_count == 2
    assert m.headers["Authorization"] == "Bearer prs_cli_renewed"
    assert token_path.read_text() == "prs_cli_renewed"


def test_env_token_401_is_not_reauthenticated(tmp_path, monkeypatch):
    """A 401 with a caller-supplied PRAISE_TOKEN is surfaced, not silently replaced."""
    monkeypatch.setenv("PRAISE_TOKEN", "prs_cli_fromenv")
    m = _session_mock()
    m.get.return_value = _resp(401, {"success": False, "error": {"code": "apiError.sessionExpired"}})

    with mock.patch("praiselul.praise.praise_session.requests.Session", return_value=m):
        with _make_session(tmp_path) as session:
            with pytest.raises(requests.HTTPError):
                session.get_timesheet(year=2026, month=6)

    m.post.assert_not_called()
    assert m.get.call_count == 1
