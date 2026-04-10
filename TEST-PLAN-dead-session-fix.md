# Test Plan — Dead Session Bail Fix (#19)

**Status:** ✅ **TESTED. 8/8 tests passing.** Ready to commit to OSS.
**Date:** 2026-04-10
**Goal:** Validate the InvalidStatus handler and _DEAD_SESSIONS denylist before pushing to OSS

## Test results

```
$ python -m pytest tests/ -v
tests/test_dead_session_bail.py::test_T2_bail_after_three_consecutive_401s  PASSED
tests/test_dead_session_bail.py::test_T3_bail_after_three_consecutive_403s  PASSED
tests/test_dead_session_bail.py::test_T1_single_401_does_not_bail           PASSED
tests/test_dead_session_bail.py::test_T4_500_stays_transient_does_not_bail  PASSED
tests/test_dead_session_bail.py::test_T5_counter_resets_on_successful_connect  PASSED
tests/test_dead_session_bail.py::test_T7_normal_connection_no_regression    PASSED
tests/test_dead_session_bail.py::test_T6_dead_sessions_is_module_level_set  PASSED
tests/test_dead_session_bail.py::test_T6_scan_loop_contains_dead_sessions_skip  PASSED
8 passed in 0.23s
```

**Mutation test:** Deliberately broke the fix (changed `MAX_HANDSHAKE_AUTH_FAILS = 3` to `999`). T2 and T3 failed immediately with `AssertionError: expected 3, got 999`. Restored, all 8 pass again. Tests are doing real work, not just smoke-testing.

---

## Why this needs testing

The live restart of the conductor cleared 12 zombie sessions, but it cleared them the "wrong way" — the server simply stopped returning those session IDs from `/v1/code/sessions`. The bail-on-401 code path was **never actually exercised in production**. We observed:

- Before: 13 monitors, 47K drops (all HTTP 401 from zombies)
- After: 5 monitors, 42 drops (all legit 1006 from one session)

But the transition happened because the API list changed, not because our new bail logic fired. **We can't prove the fix works without a test that actually triggers the InvalidStatus exception.**

## What changed in `remote-control.py`

1. `from websockets.exceptions import InvalidStatus` added to imports
2. Module-level `_DEAD_SESSIONS: set[str] = set()` denylist
3. `SessionMonitor.connect()`:
   - New counters `handshake_auth_fails` + `MAX_HANDSHAKE_AUTH_FAILS = 3`
   - Counter reset on successful connect
   - New `except InvalidStatus as e:` block before the generic `except Exception`
   - Logic: 401/403 → bail after 3 consecutive fails, add to `_DEAD_SESSIONS`; 5xx → transient, counter increments
4. `scan_and_connect()` skips any sid in `_DEAD_SESSIONS`

## Test paths that MUST pass

| # | Path | Expected behavior |
|---|---|---|
| T1 | `InvalidStatus(401)` first time | Counter = 1, log WARNING, continue retrying |
| T2 | `InvalidStatus(401)` third time | Counter = 3, log "Bailing", add to `_DEAD_SESSIONS`, `break` out of connect() loop |
| T3 | `InvalidStatus(403)` third time | Same as T2 but with 403 in the log |
| T4 | `InvalidStatus(500)` | `transient_drops += 1`, log WARNING with "Handshake HTTP 500", continue retrying |
| T5 | `InvalidStatus(401)` × 2, then successful connect | `handshake_auth_fails` reset to 0 on connect; session stays alive |
| T6 | `sid` in `_DEAD_SESSIONS` during scan | Session is skipped, no `New RC session` log, no monitor created |
| T7 | Normal session flow (no failures) | Works exactly as before — no regression |

## Test approaches (pick any; unit test is preferred)

### Approach 1: Unit test with mocks (RECOMMENDED)

**Pros:** Fast, deterministic, repeatable, covers all 7 test paths
**Cons:** Requires setting up pytest + mock harness for asyncio

**Setup:**
```bash
pip install pytest pytest-asyncio
```

**Test file:** `Desktop/conductor/tests/test_dead_session_bail.py`

**Mock pattern for triggering InvalidStatus:**
```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from websockets.exceptions import InvalidStatus

# Fake HTTP response object with status_code
def make_invalid_status(code: int) -> InvalidStatus:
    response = MagicMock()
    response.status_code = code
    return InvalidStatus(response)

@pytest.mark.asyncio
async def test_bail_on_three_consecutive_401s():
    from remote_control import SessionMonitor, _DEAD_SESSIONS

    _DEAD_SESSIONS.clear()  # Reset global state

    monitor = SessionMonitor(
        session_id="test_session_id",
        access_token="fake_token",
        org_uuid="fake_org",
        goals={},
        project="test-project",
    )

    # Patch websockets.connect to raise 401 three times, then we expect bail
    with patch("remote_control.websockets.connect") as mock_connect:
        mock_connect.side_effect = make_invalid_status(401)

        # Make sleeps instant so the test doesn't hang
        with patch("remote_control.asyncio.sleep", new_callable=AsyncMock):
            await monitor.connect()

    # After 3 fails, monitor should bail out
    assert "test_session_id" in _DEAD_SESSIONS
    assert mock_connect.call_count == 3  # exactly 3 attempts, not more
```

**Tests to write:**
- `test_bail_on_three_consecutive_401s()` — covers T2
- `test_bail_on_three_consecutive_403s()` — covers T3
- `test_500_is_transient_not_bail()` — covers T4 (verify call_count > 3 or some other indicator it kept retrying)
- `test_counter_resets_on_successful_connect()` — covers T5
- `test_scan_skips_dead_sessions()` — covers T6
- `test_normal_connection_unchanged()` — covers T7 regression

### Approach 2: Fault injection via fake session ID

**Pros:** Tests against real websockets library, real sleep behavior, real logging
**Cons:** Doesn't test all code paths, slower, hard to reset state

**Steps:**
1. Stop current conductor: find PID, kill it
2. Add a fake session to bridge-pointer discovery (or pass via `--session fake_session_dead123` CLI arg)
3. Start with redirected log: `python remote-control.py --session fake_session_dead123 > /tmp/rc-test.log 2>&1 &`
4. Wait ~3 minutes (3 retries with 60s backoff)
5. `grep "Dead session" /tmp/rc-test.log` — should see the bail message
6. `grep "BAIL.*dead session" ~/.claude/conductor/log.md` — should see it logged to log.md
7. Kill test process, restore normal conductor

**What this tests:** T2 (bail path), not the denylist or 5xx path.

### Approach 3: Monkey-patch at module load

**Pros:** Real code, real asyncio, real scan loop
**Cons:** Fragile, hard to assert against

Create a test harness that imports `remote_control`, patches `websockets.connect`, runs the main loop for a few seconds, and checks `_DEAD_SESSIONS` contents.

```python
# test_dead_session_integration.py
import asyncio
import remote_control
from unittest.mock import patch, MagicMock
from websockets.exceptions import InvalidStatus

response = MagicMock()
response.status_code = 401
err = InvalidStatus(response)

async def main():
    with patch.object(remote_control.websockets, "connect", side_effect=err):
        # Run the main loop for 10s then stop
        task = asyncio.create_task(remote_control.run_main())
        await asyncio.sleep(10)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    print(f"Dead sessions: {remote_control._DEAD_SESSIONS}")

asyncio.run(main())
```

## Commit checklist

Before `git add remote-control.py && git commit` in `Desktop/conductor/`:

- [ ] At least T1-T4 pass (either via unit tests or fault injection)
- [ ] T7 (regression — normal connection) verified — live conductor has been running with the patch for at least 1 hour with no new issues
- [ ] `python -m py_compile remote-control.py` — syntax OK
- [ ] Local git diff reviewed: ~44 lines added, 0 removed
- [ ] Commit message includes: what changed, why, before/after metrics
- [ ] Do NOT skip pre-commit hooks

## Evidence we already have (insufficient but reassuring)

- ✅ Syntax check passes for both `~/.claude/conductor/` and `Desktop/conductor/` versions
- ✅ Live conductor has been running with the patch since 12:32 EDT 2026-04-10
- ✅ 5 real sessions connect successfully (T7 regression smoke test)
- ✅ No new HTTP 401 errors in log (good, but not a proof since API stopped returning zombies)
- ❌ Bail path (T2/T3) never fired because no 401 has occurred since the patch
- ❌ Denylist (T6) never exercised because _DEAD_SESSIONS is empty
- ❌ 5xx path (T4) never fired because no 5xx has occurred

## If a test fails

**T2/T3 fails (bail doesn't trigger):**
- Check that `InvalidStatus` is being caught BEFORE the generic `except Exception`
- Exception order matters in Python — a broader catch earlier will swallow the narrower one
- Verify `status_code` attribute is accessed correctly via `getattr(e.response, "status_code", None)`

**T4 fails (5xx bails instead of staying transient):**
- The `if status in (401, 403)` check is wrong — 500 shouldn't enter that branch
- Verify the `else` branch just increments `transient_drops`

**T5 fails (counter doesn't reset):**
- The `handshake_auth_fails = 0` reset must be inside the `async with websockets.connect` block AFTER the successful connection log, not outside

**T6 fails (denylist skipped):**
- Check the order of checks in `scan_and_connect` — `_DEAD_SESSIONS` check must come AFTER the `active_monitors` check but BEFORE `session_friendly_name`
- Verify `_DEAD_SESSIONS` is module-level (not instance-level) so the scan loop sees the same set the monitor populated

## Rollback plan

If a regression is discovered after this fix ships, revert the fix from
this repo's history and redeploy:

```bash
# In your Conductor clone:
git revert <commit-sha-of-this-fix>

# Then copy the reverted remote-control.py to your live conductor location
# (wherever you run it from — e.g. ~/.claude/conductor/) and restart the
# remote-control.py process.
```

The bail logic is scoped to the `InvalidStatus` except block + the
`_DEAD_SESSIONS` scan guard. Reverting those two sections restores the
pre-fix behavior exactly (infinite retries on HTTP 401, no denylist).
