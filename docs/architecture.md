# Architecture

## System Overview

```
+----------------------------------------------------------+
|                  CONDUCTOR SESSION                        |
|                                                          |
|  bridge-server.py (MCP, stdio)                           |
|    13 tools: monitor + decide + block + discover + send  |
|                                                          |
|  remote-control.py (persistent async service)            |
|    WebSocket per --rc session                            |
|    Auto-approve safe tools, escalate dangerous ones      |
|    Full conversation stream visibility                   |
|                                                          |
+----+----------+----------+----------+--------------------+
     |          |          |          |
  +--v--+   +--v--+   +--v--+   +--v--+
  | T1  |   | T2  |   | T3  |   | T4  |   <- claude --rc
  +--+--+   +--+--+   +--+--+   +--+--+
     |          |          |          |
     | WS       | WS       | WS       | WS    WebSocket (approve/stream)
     | hook     | hook     | hook     | hook   conductor-guard.js (block)
     +----------+----------+----------+
```

## Two Monitoring Paths

### Path 1: JSONL (works with all sessions)

Claude Code writes conversation logs to `~/.claude/projects/{dir}/{session-id}.jsonl`. The MCP server reads these files to:

- List active sessions (cross-reference with `~/.claude/sessions/*.json` for PIDs)
- Read recent messages and tool calls
- Detect stalls by analyzing timestamps and `stop_reason` values
- No setup required — works with any Claude Code session

### Path 2: WebSocket (requires `--rc` sessions)

Sessions started with `claude --rc` register with Anthropic's API. The conductor subscribes via WebSocket to:

- Receive tool approval requests in real-time
- Auto-approve or deny based on decision logic
- Stream the full conversation (every message, tool call, result)
- Send messages/tasks to the session

WebSocket is faster and richer, but requires Remote Control to be active.

## Components

### bridge-server.py

MCP server using stdio transport. Launched by Claude Code on demand (one process per session lifetime). Handles JSON-RPC 2.0 protocol.

**Data sources:**
- `~/.claude/sessions/*.json` — local session metadata (PID, UUID, cwd)
- `~/.claude/projects/{dir}/{uuid}.jsonl` — conversation logs
- `~/.claude/projects/{dir}/bridge-pointer.json` — server-side session IDs
- `~/.claude/.credentials.json` — OAuth tokens (for API calls)
- `~/.claude.json` — org UUID
- `~/.claude/conductor/goals.json` — session goals
- `~/.claude/conductor/flags/*.json` — per-session control flags

### remote-control.py

Persistent async Python service. Runs independently (not an MCP server). Connects to Anthropic's WebSocket API for each `--rc` session.

**Loop:**
1. Scan for RC sessions every 30 seconds
2. Connect WebSocket to new sessions
3. Handle `initialize` handshake
4. Receive `can_use_tool` requests → approve or escalate
5. Clean up disconnected sessions
6. Refresh OAuth tokens as needed

### conductor-guard.js

PreToolUse hook installed globally in `~/.claude/settings.json`. Runs before every Bash/Write/Edit/Agent tool call in ALL sessions.

**Two layers:**
1. Pattern matching — always blocks dangerous commands (hardcoded)
2. Flag files — conductor can block/unblock any session via `flags/{session-id}.json`

**Blocking mechanism:** Outputs `{"decision": "block"}` on stdout at exit code 0. Claude Code reads this JSON and denies the tool execution.

## Decision Framework

When a session is stalled on a tool approval:

```
Read goal + risk level
  |
  v
Is tool read-only? (Read, Glob, Grep, git log) --> APPROVE
  |
  v
Is it a file write/edit? --> APPROVE
  |
  v
Is it Bash in a low-risk session? --> APPROVE
  |
  v
Is it a git commit in low/medium-risk? --> APPROVE
  |
  v
Is it destructive? (force push, rm -rf, DROP TABLE) --> ESCALATE
  |
  v
Is it Bash in a high-risk session? --> ESCALATE
  |
  v
No goal registered? --> ESCALATE
  |
  v
Default with goal --> APPROVE
```

## Data Flow

### Tool approval (happy path)

```
Session: "I want to run git commit"
  |
  v (WebSocket)
Conductor receives can_use_tool
  |
  v
make_decision: goal="build API", risk=low, tool=Bash
  |
  v
Decision: APPROVE (git commit in low-risk session)
  |
  v (WebSocket)
control_response: behavior=allow
  |
  v
Session: git commit runs, no prompt shown to user
```

### Tool blocking (guard hook)

```
Session: "I want to run git push --force"
  |
  v (PreToolUse hook)
conductor-guard.js: matches DANGEROUS_PATTERNS
  |
  v
Output: {"decision": "block"}
  |
  v
Session: "Hook PreToolUse:Bash denied this tool"
(command never executes)
```

### Task injection

```
Conductor: send_task("session_abc", "run the test suite")
  |
  v (HTTP POST)
POST /v1/sessions/session_abc/events
  |
  v
Target session: receives "run the test suite" as user input
  |
  v
Claude processes it and responds normally
```
