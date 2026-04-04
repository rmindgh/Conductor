# Conductor

A multi-session orchestration system for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

Run multiple Claude Code terminals. Monitor all of them from one place. Auto-approve safe tool calls. Block dangerous ones. Send tasks to any session. Get Telegram alerts when something needs your attention.

## What it does

You run 4+ Claude Code sessions doing different work. Conductor sits in a fifth session and:

- **Sees** what every session is doing (real-time via WebSocket or JSONL polling)
- **Approves** safe tool calls so sessions don't stall waiting for you to press Y
- **Blocks** dangerous commands (force push, rm -rf) before they run
- **Sends tasks** to any session — type a message here, it appears there
- **Alerts you** via Telegram when something needs human judgment

```
         You (desk or mobile)
              |
    +---------+---------+
    |    CONDUCTOR       |
    |                    |
    |  monitor + decide  |
    |  approve + block   |
    |  send tasks        |
    +--+----+----+----+--+
       |    |    |    |
      T1   T2   T3   T4     <- claude --rc sessions
```

## How it works

Four layers, each independent:

| Layer | What | How |
|-------|------|-----|
| **Awareness** | See all sessions, detect stalls | Reads Claude Code's JSONL conversation logs + session files |
| **Decisions** | Recommend approve/deny based on goals | Goal registry + risk levels + decision framework |
| **Guardrails** | Block dangerous commands | PreToolUse hook with `{"decision": "block"}` output |
| **Remote Control** | Approve tools + send messages | WebSocket subscription to Claude Code's Remote Control API |

## Quick start

### Prerequisites

- Claude Code v2.1.92+ (`claude update`)
- Python 3.10+
- `websockets` and `httpx` packages (`pip install websockets httpx`)
- Claude Max or Team subscription (for Remote Control)

### 1. Install the MCP server

Copy `bridge-server.py` somewhere permanent:

```bash
mkdir -p ~/.claude/conductor
cp bridge-server.py ~/.claude/conductor/
```

Add to your Claude Code config (`~/.claude.json`) under your project's `mcpServers`:

```json
{
  "projects": {
    "your/project/path": {
      "mcpServers": {
        "conductor": {
          "command": "python",
          "args": ["/path/to/.claude/conductor/bridge-server.py"]
        }
      }
    }
  }
}
```

Add permissions in `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["mcp__conductor__*"]
  }
}
```

### 2. Install the guard hook

Copy `hooks/conductor-guard.js` to `~/.claude/conductor/hooks/`.

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit|Agent",
        "hooks": [
          {
            "type": "command",
            "command": "node \"/path/to/.claude/conductor/hooks/conductor-guard.js\"",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### 3. Start sessions with Remote Control

```bash
claude --rc
```

Sessions started with `--rc` register with Anthropic's API and can be monitored via WebSocket.

### 4. Use from the conductor session

```
> discover sessions
> what is the rentcompare session doing?
> send "run the test suite" to the rentcompare session
> block the trading session
```

Or set up a monitoring loop:

```
/loop 3m check all sessions, report changes, approve safe stalls
```

## MCP Tools (13 total)

### Awareness

| Tool | Purpose |
|------|---------|
| `list_sessions` | All local sessions — PID, project, duration, last message |
| `get_activity(session_id, last_n)` | Recent messages + tool calls from a session |
| `get_status(session_id)` | Processing / waiting / stuck on approval |
| `get_all_waiting` | Sessions needing attention |

### Decisions

| Tool | Purpose |
|------|---------|
| `set_goal(session_id, goal, risk_level)` | Register what a session is working on |
| `get_goals` | List all session goals |
| `make_decision(session_id)` | Approve/escalate recommendation based on context |
| `log_event(event)` | Append to activity log |

### Guardrails

| Tool | Purpose |
|------|---------|
| `set_flag(session_id, action, reason)` | Block or unblock a session |
| `clear_flag(session_id)` | Remove a block |
| `get_flags` | List active blocks |

### Remote Control

| Tool | Purpose |
|------|---------|
| `discover_rc_sessions` | Find all `--rc` sessions with connection status |
| `send_task(session_id, message)` | Send a message/task to a session |

## Stall detection

Conductor detects three patterns that indicate a session is stuck:

1. **`stop_reason: "tool_use"` with no result** — Claude called a tool that needs approval. Most reliable signal.
2. **Stale `progress` entries** — Session was producing events but stopped for >90 seconds.
3. **Empty `user` messages from hooks** — Artifacts that mask the real last event. Conductor skips them.

Threshold is configurable: `STALL_THRESHOLD_SECONDS = 90` in `bridge-server.py`.

## Hook blocking

Claude Code's PreToolUse hooks can block tool execution. The documented approach:

| Method | Exit code | Output | Blocks? |
|--------|-----------|--------|---------|
| Normal exit | 0 | Any | No |
| Error exit | 1 | Any | **No** (advisory only) |
| Block exit | 2 | Any | **Yes** |
| JSON block | 0 | `{"decision": "block"}` | **Yes** |

Conductor uses `{"decision": "block"}` at exit 0. The hook receives full tool context on stdin:

```json
{
  "session_id": "local-uuid",
  "tool_name": "Bash",
  "tool_input": {"command": "git push --force"},
  "hook_event_name": "PreToolUse"
}
```

The guard hook blocks dangerous patterns (force push, rm -rf, DROP TABLE) and checks per-session flag files for conductor-controlled blocking.

## Remote Control API

Claude Code sessions started with `--rc` register with Anthropic's servers. The conductor can subscribe via WebSocket and handle tool approval requests programmatically.

See [docs/protocol.md](docs/protocol.md) for the full protocol documentation.

**Key points:**
- Sessions get a server-side ID (`session_*` format) stored in `bridge-pointer.json`
- WebSocket at `wss://api.anthropic.com/v1/sessions/ws/{id}/subscribe`
- Must respond to `initialize` control request within ~10 seconds
- Tool approvals via `control_response` with `behavior: "allow"` or `"deny"`
- Can inject user messages via `POST /v1/sessions/{id}/events`

## Files

```
conductor/
  bridge-server.py       # MCP server (Python, zero dependencies)
  remote-control.py      # WebSocket auto-approve service (requires websockets, httpx)
  hooks/
    conductor-guard.js   # PreToolUse hook (Node.js, zero dependencies)
  docs/
    protocol.md          # Remote Control API protocol
    architecture.md      # System design
```

Runtime files (created automatically):

```
~/.claude/conductor/
  goals.json             # Session goals + risk levels
  decisions.json         # Decision log (last 100)
  log.md                 # Activity log
  flags/                 # Per-session block/proceed flags
```

## Limitations

- **Remote Control requires `claude --rc`** — regular sessions can only be monitored via JSONL, not auto-approved
- **WebSocket must connect before approval prompt** — can't retroactively approve a prompt that's already waiting
- **Tool approval shows tool name but not always the full command** in JSONL — WebSocket stream has full context
- **OAuth tokens expire** — conductor reads fresh tokens from `~/.claude/.credentials.json` automatically
- **Undocumented APIs** — the Remote Control WebSocket protocol and hook blocking mechanism are based on Claude Code's internal architecture. They may change between versions.

## Background

This started as a simple question: "Can you see what my other terminal is doing?"

From there it grew into a full orchestration system over two sessions:
- Phase 1: Reading JSONL files to detect session state
- Phase 2: Decision framework + Telegram alerts
- Phase 3: Discovering that `exit(1)` doesn't block hooks (but `exit(2)` and `{"decision": "block"}` do)
- Phase 4: Finding the Remote Control WebSocket API, cracking the subscription protocol, and sending messages to sessions

The Remote Control API details were found by studying Claude Code's source. The hook blocking behavior was discovered through testing and source analysis. The JSONL format was reverse-engineered by reading the files Claude Code produces.

None of this is officially documented by Anthropic. Use accordingly.

## Contributing

This is a proof of concept. PRs welcome, especially for:
- Better session discovery (auto-detect `--rc` sessions without API polling)
- Persistent WebSocket service improvements
- Support for macOS Keychain credentials (currently Windows/Linux only)
- Dashboard UI for monitoring
- Task chaining (session A finishes -> assign next task to session B)

## License

MIT
