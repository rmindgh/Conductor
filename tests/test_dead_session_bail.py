"""
Unit tests for the dead-session bail fix in remote-control.py.

Covers test paths T1-T5 and T7 from TEST-PLAN-dead-session-fix.md:
  T1: InvalidStatus(401) first time → counter = 1, continue retrying
  T2: InvalidStatus(401) third time → bail, add to _DEAD_SESSIONS
  T3: InvalidStatus(403) third time → same as T2
  T4: InvalidStatus(500) → transient, no bail
  T5: 2× 401, then successful connect → counter resets
  T7: Normal connection (no failures) → works as before

T6 (scan loop denylist skip) is tested by static inspection + a simple
behavioral test, since scan_and_connect is a nested function inside run().

Run:
    cd Desktop/conductor
    python -m pytest tests/ -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import InvalidStatus

import remote_control  # loaded via conftest.py


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_invalid_status(status_code: int) -> InvalidStatus:
    """Construct an InvalidStatus exception with a mocked HTTP response."""
    response = MagicMock()
    response.status_code = status_code
    return InvalidStatus(response)


def make_monitor(session_id: str = "session_test_dead_001") -> "remote_control.SessionMonitor":
    """Build a SessionMonitor with fake credentials for testing."""
    return remote_control.SessionMonitor(
        session_id=session_id,
        access_token="fake_token_for_testing",
        org_uuid="fake_org_uuid",
        goals={},
        project="test-project",
    )


class FakeWebSocket:
    """
    Minimal async context manager that behaves enough like a websockets
    connection for SessionMonitor.connect() to run through the happy path
    once. After yielding a single initialize message (or nothing), the
    async-with exits, which the connect() loop treats as a normal close
    and reconnects.
    """

    def __init__(self, messages=None):
        self._messages = messages or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for msg in self._messages:
            yield msg


# ---------------------------------------------------------------------------
# Tests for InvalidStatus handling
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state between tests."""
    remote_control._DEAD_SESSIONS.clear()
    remote_control._RC_STATS["drops"] = 0
    remote_control._RC_STATS["monitors"] = 0
    remote_control._RC_STATS["escalations"] = 0
    yield
    remote_control._DEAD_SESSIONS.clear()


@pytest.mark.asyncio
async def test_T2_bail_after_three_consecutive_401s():
    """T2: Three consecutive HTTP 401 at handshake → bail, add to denylist."""
    monitor = make_monitor(session_id="session_test_t2_401")

    with patch.object(
        remote_control.websockets,
        "connect",
        side_effect=make_invalid_status(401),
    ) as mock_connect:
        # Make sleep instant so the test doesn't wait for exponential backoff
        with patch.object(
            remote_control.asyncio, "sleep", new=AsyncMock()
        ):
            await asyncio.wait_for(monitor.connect(), timeout=3.0)

    assert mock_connect.call_count == 3, (
        f"Expected exactly 3 connection attempts before bail, "
        f"got {mock_connect.call_count}"
    )
    assert "session_test_t2_401" in remote_control._DEAD_SESSIONS, (
        "Bailed session must be added to _DEAD_SESSIONS denylist"
    )


@pytest.mark.asyncio
async def test_T3_bail_after_three_consecutive_403s():
    """T3: Three consecutive HTTP 403 at handshake → bail, same as 401 path."""
    monitor = make_monitor(session_id="session_test_t3_403")

    with patch.object(
        remote_control.websockets,
        "connect",
        side_effect=make_invalid_status(403),
    ) as mock_connect:
        with patch.object(remote_control.asyncio, "sleep", new=AsyncMock()):
            await asyncio.wait_for(monitor.connect(), timeout=3.0)

    assert mock_connect.call_count == 3
    assert "session_test_t3_403" in remote_control._DEAD_SESSIONS


@pytest.mark.asyncio
async def test_T1_single_401_does_not_bail():
    """T1: One HTTP 401 → counter = 1, loop continues (does not bail)."""
    monitor = make_monitor(session_id="session_test_t1_single")

    # First 2 calls raise 401, third would bail. We stop before 3rd.
    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise make_invalid_status(401)
        # Third call: raise CancelledError to exit the loop cleanly
        raise asyncio.CancelledError()

    with patch.object(
        remote_control.websockets, "connect", side_effect=side_effect
    ):
        with patch.object(remote_control.asyncio, "sleep", new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await monitor.connect()

    # After only 2 failures, session should NOT be in denylist yet
    assert "session_test_t1_single" not in remote_control._DEAD_SESSIONS, (
        "Session should NOT be bailed after only 2 fails (threshold is 3)"
    )
    assert call_count["n"] == 3, "Expected 3 call attempts (2 fails + 1 cancel)"


@pytest.mark.asyncio
async def test_T4_500_stays_transient_does_not_bail():
    """T4: HTTP 500 at handshake → transient, counter increments, no bail."""
    monitor = make_monitor(session_id="session_test_t4_500")

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 5:
            raise make_invalid_status(500)
        # After 5 transient fails, cancel the loop to end the test
        raise asyncio.CancelledError()

    with patch.object(
        remote_control.websockets, "connect", side_effect=side_effect
    ):
        with patch.object(remote_control.asyncio, "sleep", new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await monitor.connect()

    assert "session_test_t4_500" not in remote_control._DEAD_SESSIONS, (
        "HTTP 500 must NOT trigger bail — it's a real server issue, not a dead session"
    )
    # Loop kept trying past the 3-attempt threshold → proves 5xx is transient
    assert call_count["n"] == 6, (
        f"Expected 5 transient fails + 1 cancel = 6 attempts, got {call_count['n']}"
    )
    # transient_drops is local to connect(), but _RC_STATS["drops"] should
    # have been bumped on each retry
    assert remote_control._RC_STATS["drops"] >= 5, (
        f"Expected drops stat to reflect 5+ transient fails, "
        f"got {remote_control._RC_STATS['drops']}"
    )


@pytest.mark.asyncio
async def test_T5_counter_resets_on_successful_connect():
    """
    T5: 2× 401, then successful connect, then 2× 401 again → no bail.
    If the counter did NOT reset, the 4th total failure would bail.
    If the counter DID reset, we need 3 more fails post-reconnect to bail.
    """
    monitor = make_monitor(session_id="session_test_t5_reset")

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        n = call_count["n"]
        # 1-2: 401
        if n in (1, 2):
            raise make_invalid_status(401)
        # 3: successful connect (fake ws that exits the async-for loop immediately)
        if n == 3:
            return FakeWebSocket(messages=[])
        # 4-5: 401 again (only 2 failures post-reconnect, should NOT bail)
        if n in (4, 5):
            raise make_invalid_status(401)
        # 6: cancel to end test
        raise asyncio.CancelledError()

    with patch.object(
        remote_control.websockets, "connect", side_effect=side_effect
    ):
        with patch.object(remote_control.asyncio, "sleep", new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await monitor.connect()

    assert "session_test_t5_reset" not in remote_control._DEAD_SESSIONS, (
        "Session should NOT be bailed: counter should have reset on the successful "
        "connect (attempt 3), so attempts 4-5 only reached 2 post-reconnect fails, "
        "below the bail threshold of 3. If the counter didn't reset, we'd bail on "
        "attempt 5 (2 pre + 1 post = 3 total)."
    )
    assert call_count["n"] == 6, (
        f"Expected all 6 attempts to run, got {call_count['n']}"
    )


@pytest.mark.asyncio
async def test_T7_normal_connection_no_regression():
    """T7: Successful connect → no bail, no fails. Regression test."""
    monitor = make_monitor(session_id="session_test_t7_normal")

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeWebSocket(messages=[])
        # Second attempt: cancel to exit
        raise asyncio.CancelledError()

    with patch.object(
        remote_control.websockets, "connect", side_effect=side_effect
    ):
        with patch.object(remote_control.asyncio, "sleep", new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await monitor.connect()

    assert "session_test_t7_normal" not in remote_control._DEAD_SESSIONS
    # Successful first connect → handshake_auth_fails never reached even 1


# ---------------------------------------------------------------------------
# T6: Denylist static check
# ---------------------------------------------------------------------------

def test_T6_dead_sessions_is_module_level_set():
    """
    T6 (indirect): _DEAD_SESSIONS must be module-level and of type set
    so the scan loop and monitor instances share it. A type mismatch or
    instance-level declaration would silently break the denylist.
    """
    assert hasattr(remote_control, "_DEAD_SESSIONS"), (
        "_DEAD_SESSIONS must exist at module level"
    )
    assert isinstance(remote_control._DEAD_SESSIONS, set), (
        f"_DEAD_SESSIONS must be a set, got {type(remote_control._DEAD_SESSIONS)}"
    )


def test_T6_scan_loop_contains_dead_sessions_skip():
    """
    T6 (static): Verify the scan_and_connect function body contains the
    `if sid in _DEAD_SESSIONS: continue` guard. This is a source-level
    check since scan_and_connect is a nested function that can't be unit
    tested in isolation without a major refactor.
    """
    import inspect
    source = inspect.getsource(remote_control.run)
    assert "_DEAD_SESSIONS" in source, (
        "run() function must reference _DEAD_SESSIONS (scan loop guard)"
    )
    assert "sid in _DEAD_SESSIONS" in source, (
        "Scan loop must have the 'if sid in _DEAD_SESSIONS' check"
    )


# ---------------------------------------------------------------------------
# T8-T11: discover_all_rc_sessions — API is authoritative, local is enrichment
#
# Regression tests for the self-monitoring loop bug where
# discover_all_rc_sessions picked up non-RC sessions from local
# bridge-pointer.json files and tried to subscribe to them, causing
# endless 1006 reconnect cycles against the conductor's own parent
# Claude Code REPL session.
# ---------------------------------------------------------------------------

def test_T8_discover_api_only_session_is_included():
    """T8: A session in the API (and not in local) should be included."""
    with patch.object(
        remote_control,
        "discover_rc_sessions_api",
        return_value=[
            {
                "sessionId": "session_api_only_001",
                "cseId": "cse_api_only_001",
                "connectionStatus": "connected",
                "project": "api-project",
                "createdAt": "2026-04-10T00:00:00Z",
                "source": "api",
            }
        ],
    ):
        with patch.object(remote_control, "discover_rc_sessions_local", return_value=[]):
            result = remote_control.discover_all_rc_sessions("tok", "org")

    assert len(result) == 1
    assert result[0]["sessionId"] == "session_api_only_001"


def test_T9_discover_local_only_session_is_EXCLUDED():
    """
    T9 (the core regression test): A session present ONLY in local
    bridge-pointer.json but NOT in the API must be EXCLUDED. This was
    the bug that caused the 1006 self-monitoring loop on the conductor's
    own parent Claude Code session.
    """
    with patch.object(remote_control, "discover_rc_sessions_api", return_value=[]):
        with patch.object(
            remote_control,
            "discover_rc_sessions_local",
            return_value=[
                {
                    "sessionId": "session_local_only_repl_666",
                    "environmentId": "env_fake",
                    "project": "Claude",
                    "projectDir": "/fake/path/Claude",
                    "source": "bridge-pointer",
                }
            ],
        ):
            result = remote_control.discover_all_rc_sessions("tok", "org")

    assert result == [], (
        "Local-only sessions (bridge-pointer without API entry) must be "
        "EXCLUDED. If included, the conductor will try to subscribe to "
        "non-RC sessions and hit the 1006 reconnect loop."
    )


def test_T10_discover_api_session_is_enriched_with_local_project_name():
    """
    T10: When a session is in both API and local, the local project name
    should enrich the API entry. This preserves the valuable part of
    local discovery (human-readable project names) without reintroducing
    the self-monitoring bug.
    """
    sid = "session_both_001"
    with patch.object(
        remote_control,
        "discover_rc_sessions_api",
        return_value=[
            {
                "sessionId": sid,
                "cseId": "cse_both_001",
                "connectionStatus": "connected",
                "project": "unknown",  # API title is generic
                "createdAt": "2026-04-10T00:00:00Z",
                "source": "api",
            }
        ],
    ):
        with patch.object(
            remote_control,
            "discover_rc_sessions_local",
            return_value=[
                {
                    "sessionId": sid,
                    "environmentId": "env_123",
                    "project": "rentcompare",  # nice local project name
                    "projectDir": "/fake/projects/rentcompare",
                    "source": "bridge-pointer",
                }
            ],
        ):
            result = remote_control.discover_all_rc_sessions("tok", "org")

    assert len(result) == 1
    assert result[0]["project"] == "rentcompare", (
        "Local project name should enrich the API entry"
    )
    assert result[0]["projectDir"] == "/fake/projects/rentcompare"
    assert result[0]["connectionStatus"] == "connected"


def test_T11_discover_disconnected_api_session_is_filtered_out():
    """
    T11: Sessions with connectionStatus='disconnected' must be filtered
    out — they're stale in the API but not subscribable. Only 'connected'
    or empty status should pass through.
    """
    with patch.object(
        remote_control,
        "discover_rc_sessions_api",
        return_value=[
            {
                "sessionId": "session_active_001",
                "cseId": "cse_active_001",
                "connectionStatus": "connected",
                "project": "active-project",
            },
            {
                "sessionId": "session_stale_002",
                "cseId": "cse_stale_002",
                "connectionStatus": "disconnected",
                "project": "stale-project",
            },
        ],
    ):
        with patch.object(remote_control, "discover_rc_sessions_local", return_value=[]):
            result = remote_control.discover_all_rc_sessions("tok", "org")

    assert len(result) == 1
    assert result[0]["sessionId"] == "session_active_001"
