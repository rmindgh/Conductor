# Changelog

All notable changes to Conductor will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Self-monitoring loop on non-RC sessions.** `discover_all_rc_sessions()`
  previously included sessions found only in local `bridge-pointer.json`
  files (without API confirmation) and tried to WebSocket-subscribe to
  them. The conductor's own parent Claude Code REPL session has a
  bridge-pointer file but is NOT an RC session, so the server accepted
  the upgrade and then closed with code 1006 after ~5s because no RC
  protocol exists — producing an endless reconnect loop (~7-8s period).
  The API is now authoritative for session discovery; local
  bridge-pointer files are used only to enrich API results with friendly
  project names and cwd info.

### Added
- Unit tests T8-T11 covering `discover_all_rc_sessions()`: API-only
  inclusion, local-only exclusion (the regression case), API+local
  enrichment, and disconnected-status filtering.

## [0.2.0] - 2026-04-10

### Added
- `_DEAD_SESSIONS` module-level denylist so dead sessions can't be endlessly
  re-discovered by the scan loop after bailing.
- `tests/` directory with 8 pytest unit tests covering the InvalidStatus
  handler and denylist logic (T1-T7 in the test plan). Mutation-tested to
  confirm the tests exercise real behavior.
- `TEST-PLAN-dead-session-fix.md` documenting the test strategy, the 7 test
  paths, commit checklist, and rollback plan.
- `SECURITY.md` documenting OAuth token handling, threat model, and the
  explicit limits of the bash command analyzer.
- Live `rc-status.json` stats file (`{monitors, drops, escalations,
  updated_at}`) for statusline consumers to read without polling the log.
- Auto-snapshot system — every ~5 minutes, scans active session JSONLs from
  the last 10 minutes and extracts recent user tasks + assistant activity
  into `.conductor-snapshot.md` files in each project directory.
- Resilient WebSocket reconnection with exponential backoff + jitter (2s →
  60s cap) replacing the previous 5-retry hard limit.

### Fixed
- **Dead sessions now bail on HTTP 401 at WebSocket handshake.** Sessions
  whose IDs the server no longer recognized returned HTTP 401 during the
  WebSocket upgrade, which the generic `except Exception` clause caught as
  a transient network error. On one stale conductor, this accumulated
  47,679 "drops" over 5 days — 99.7% of which were handshake 401s on
  zombie session IDs, not real WebSocket close codes. Fix catches
  `InvalidStatus` explicitly: HTTP 401/403 bails after 3 consecutive
  fails (same margin as the existing 4001 session-gone logic); HTTP 5xx
  stays transient. Counter resets on successful connect.
- **WebSocket 1006 death cycle.** The previous 5-retry hard limit gave up
  permanently after ~30s of flaky connection. Now retries indefinitely
  with exponential backoff + jitter, differentiating transient drops
  (1006, network errors) from dead sessions (4001 × 3) and auth failures
  (4003). Lowered `ping_interval` to 20s to stay ahead of server idle
  timeouts.
- **Bash command analysis bypasses.** Replaced substring/regex matching
  with `shlex.split` (Python) and a naive shell tokenizer (Node) in both
  the auto-approve layer and the hook. Now catches `git push -fu`
  (combined flag clusters), `rm -rf` / `rm -fr` / `rm -r -f` variants,
  and `VAR=value` prefixes.
- **Latent `PROJECTS_DIR` undefined bug** in `remote-control.py` —
  `discover_rc_sessions_local()` referenced the constant but it was
  never declared in this file.

### Changed
- README: emphasize coherent single-session awareness.
- Hook is deliberately less strict than the auto-approve layer: blocks
  only catastrophic commands (`rm -rf /`, `git push --force`, `git reset
  --hard`, `DROP DATABASE`) so legitimate workflows like `bash -c` and
  `curl | sh` still pass through.

### Security
- Added Threat Model section to SECURITY.md making explicit what the bash
  analyzer protects against (honest mistakes) vs what it does not
  (adversarial input via variable expansion, command substitution,
  encoded payloads, exec wrappers). The real security boundaries are:
  OAuth token storage, human approval, and the conductor flag system.
  The analyzer is the last layer, not the primary defense.

## [0.1.0] - 2026-04-04

### Added
- Initial public release of Conductor — multi-session orchestration for
  Claude Code.
- **Phase 1 — Awareness:** MCP tools for session discovery and state
  inspection (`list_sessions`, `get_activity`, `get_status`,
  `get_all_waiting`). Reads JSONL transcripts and session files.
- **Phase 2 — Decision Advisory:** Session goals, decision framework,
  Telegram escalation (`set_goal`, `get_goals`, `make_decision`,
  `log_event`). Per-session risk levels and decision log.
- **Phase 3 — Hook Guardrails:** PreToolUse hook that blocks dangerous
  commands via `{"decision":"block"}`. Per-session flags (`set_flag`,
  `clear_flag`, `get_flags`).
- **Phase 4 — Remote Control API:** WebSocket client for programmatic
  tool approval via Claude Code's undocumented sessions API at
  `wss://api.anthropic.com/v1/sessions/ws/{id}/subscribe`. Auto-approves
  safe tools, escalates dangerous ones, receives the full conversation
  stream in real time.
- `bridge-server.py` — MCP server with 12 tools covering all 4 phases.
- `remote-control.py` — persistent WebSocket client for session
  monitoring with OAuth token auth.
- `hooks/conductor-guard.js` — PreToolUse hook for command blocking at
  the Claude Code side.
- `docs/architecture.md` and `docs/protocol.md` — architecture and
  WebSocket protocol documentation.
- `LICENSE` (MIT).

[Unreleased]: https://github.com/rmindgh/Conductor/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rmindgh/Conductor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rmindgh/Conductor/releases/tag/v0.1.0
