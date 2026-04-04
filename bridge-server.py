#!/usr/bin/env python3
"""
Claude Bridge — MCP Server for Cross-Session Orchestration
Phase 1: Read-only monitoring | Phase 2: Decision advisory + logging

Transport: stdio (JSON-RPC 2.0, newline-delimited)
Dependencies: None (Python stdlib only)

Phase 1 tools:
  - list_sessions, get_activity, get_status, get_all_waiting
Phase 2 tools:
  - set_goal, get_goals, make_decision, log_event
"""

import sys
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone

# Seconds of silence after last progress event before flagging as possibly stuck
STALL_THRESHOLD_SECONDS = 90

CLAUDE_HOME = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_HOME / "sessions"
PROJECTS_DIR = CLAUDE_HOME / "projects"
CONDUCTOR_DIR = CLAUDE_HOME / "conductor"
GOALS_FILE = CONDUCTOR_DIR / "goals.json"
DECISIONS_FILE = CONDUCTOR_DIR / "decisions.json"
LOG_FILE = CONDUCTOR_DIR / "log.md"

# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_sessions",
        "description": (
            "List all Claude Code sessions. Shows PID, project directory, "
            "alive/dead status, duration, and last user message for each. "
            "Use this to see what all terminals are working on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "alive_only": {
                    "type": "boolean",
                    "description": "Only show sessions with running processes (default true)",
                }
            },
        },
    },
    {
        "name": "get_activity",
        "description": (
            "Get recent messages and tool calls from a specific session. "
            "Returns the last N conversation entries with timestamps, "
            "user messages, assistant responses, and tool calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session UUID (from list_sessions)",
                },
                "last_n": {
                    "type": "integer",
                    "description": "Number of recent entries to return (default 20)",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_status",
        "description": (
            "Get detailed status of a specific session: is it processing, "
            "waiting for user input, or idle? If waiting, shows what it's asking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session UUID",
                }
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_all_waiting",
        "description": (
            "Find all sessions currently waiting for user input. "
            "Returns the assistant's last message and what user said before it. "
            "Use this on a loop to catch sessions that need attention."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Phase 2 tools ---
    {
        "name": "set_goal",
        "description": (
            "Register or update the goal for a session. The conductor calls this "
            "after reading a session's initial messages to record what it's working on. "
            "Used by make_decision to evaluate if actions are within scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session UUID",
                },
                "goal": {
                    "type": "string",
                    "description": "What the session is trying to accomplish",
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "low=file ops only, medium=git+deps, high=external APIs/destructive",
                },
            },
            "required": ["session_id", "goal"],
        },
    },
    {
        "name": "get_goals",
        "description": "List all registered session goals with their risk levels.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "make_decision",
        "description": (
            "For a stalled session, read its goal + recent activity and return "
            "an approve/escalate recommendation with reasoning. The conductor uses "
            "this to decide whether to advise approval or alert the founder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session UUID that is stalled",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "log_event",
        "description": (
            "Append an event to the conductor activity log (log.md). "
            "Use for session state changes, decisions, stalls — anything worth "
            "remembering across context compression or overnight."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event": {
                    "type": "string",
                    "description": "What happened (one line)",
                },
            },
            "required": ["event"],
        },
    },
    # --- Phase 3 tools ---
    {
        "name": "set_flag",
        "description": (
            "Set a control flag for a session. The conductor-guard hook reads these "
            "flags before every tool call. Use 'block' to pause a session, 'proceed' "
            "to explicitly allow, 'block_tool' to block a specific tool type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session UUID",
                },
                "action": {
                    "type": "string",
                    "enum": ["block", "proceed", "block_tool"],
                    "description": "block=pause all tools, proceed=allow all, block_tool=block specific tool",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this flag is set (shown to the session when blocked)",
                },
                "tool": {
                    "type": "string",
                    "description": "Tool name to block (only for block_tool action)",
                },
            },
            "required": ["session_id", "action"],
        },
    },
    {
        "name": "clear_flag",
        "description": "Remove the control flag for a session, returning it to normal operation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session UUID",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_flags",
        "description": "List all active control flags across sessions.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- Phase 4 tools ---
    {
        "name": "discover_rc_sessions",
        "description": (
            "Find all Remote Control sessions by reading bridge-pointer.json files "
            "and querying the Anthropic API. Returns server-side session IDs with "
            "friendly project names and connection status."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_task",
        "description": (
            "Send a message/task to a Remote Control session. The message appears "
            "in the target session as if the user typed it. Use server-side session IDs "
            "(session_* format from discover_rc_sessions)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Server-side session ID (session_* format)",
                },
                "message": {
                    "type": "string",
                    "description": "The task or message to send",
                },
            },
            "required": ["session_id", "message"],
        },
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg):
    """Write to stderr (visible in Claude Code's debug output, not the protocol)."""
    sys.stderr.write(f"[claude-bridge] {msg}\n")
    sys.stderr.flush()


def is_pid_alive(pid):
    """Check if a Windows process with this PID is running."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def find_jsonl(session_id):
    """Find the JSONL conversation log for a session across all project dirs."""
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        jsonl = project_dir / f"{session_id}.jsonl"
        if jsonl.exists():
            return jsonl
    return None


def read_last_n_lines(filepath, n=20):
    """Read last N lines from a file efficiently (seek from end)."""
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Read a generous chunk from the end
            chunk_size = min(size, n * 8000)
            f.seek(max(0, size - chunk_size))
            content = f.read().decode("utf-8", errors="replace")
            lines = content.strip().split("\n")
            return lines[-n:]
    except Exception:
        return []


def parse_jsonl_entries(lines):
    """Parse JSONL lines into structured, readable entries."""
    entries = []
    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = data.get("type", "unknown")
        timestamp = data.get("timestamp", "")

        if entry_type == "user":
            msg = data.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content[:500]
            elif isinstance(content, list):
                text = " ".join(
                    block.get("text", "")[:300]
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )[:500]
            else:
                text = str(content)[:500]
            entries.append({"type": "user", "timestamp": timestamp, "text": text})

        elif entry_type == "assistant":
            msg = data.get("message", {})
            content = msg.get("content", [])
            texts = []
            tool_uses = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    texts.append(block["text"][:300])
                elif block.get("type") == "tool_use":
                    tool_uses.append(block.get("name", "unknown"))

            entries.append(
                {
                    "type": "assistant",
                    "timestamp": timestamp,
                    "text": " ".join(texts)[:500] if texts else "",
                    "tool_calls": tool_uses,
                    "stop_reason": msg.get("stop_reason", ""),
                }
            )

        elif entry_type == "progress":
            pd = data.get("data", {})
            entries.append(
                {
                    "type": "progress",
                    "timestamp": timestamp,
                    "detail": pd.get("type", ""),
                }
            )

    return entries


def _entry_age_seconds(timestamp_str):
    """How many seconds ago was this ISO timestamp?"""
    if not timestamp_str:
        return 999999
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0, (now - ts).total_seconds())
    except Exception:
        return 999999


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_list_sessions(args):
    alive_only = args.get("alive_only", True)
    sessions = []

    for sf in sorted(
        SESSIONS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
    ):
        try:
            with open(sf) as f:
                data = json.load(f)
        except Exception:
            continue

        pid = data.get("pid")
        session_id = data.get("sessionId", "")
        cwd = data.get("cwd", "")
        started_at = data.get("startedAt", 0)

        alive = is_pid_alive(pid)
        if alive_only and not alive:
            continue

        # Last user message
        last_msg = ""
        jsonl = find_jsonl(session_id)
        if jsonl:
            lines = read_last_n_lines(jsonl, 15)
            entries = parse_jsonl_entries(lines)
            for e in reversed(entries):
                if e["type"] == "user":
                    last_msg = e["text"][:200]
                    break

        started = datetime.fromtimestamp(started_at / 1000)
        duration_min = int((datetime.now() - started).total_seconds() / 60)
        project_name = Path(cwd).name if cwd else "unknown"

        sessions.append(
            {
                "pid": pid,
                "sessionId": session_id,
                "project": project_name,
                "cwd": cwd,
                "alive": alive,
                "startedAt": started.isoformat(),
                "durationMinutes": duration_min,
                "lastUserMessage": last_msg,
            }
        )

    return {"sessions": sessions, "count": len(sessions)}


def tool_get_activity(args):
    session_id = args["session_id"]
    last_n = args.get("last_n", 20)

    jsonl = find_jsonl(session_id)
    if not jsonl:
        return {"error": f"No conversation log found for session {session_id}"}

    lines = read_last_n_lines(jsonl, last_n * 3)
    entries = parse_jsonl_entries(lines)

    return {"sessionId": session_id, "entries": entries[-last_n:]}


def tool_get_status(args):
    session_id = args["session_id"]

    # Find session metadata
    session_data = None
    for sf in SESSIONS_DIR.glob("*.json"):
        try:
            with open(sf) as f:
                d = json.load(f)
            if d.get("sessionId") == session_id:
                session_data = d
                break
        except Exception:
            continue

    if not session_data:
        return {"error": f"Session {session_id} not found"}

    pid = session_data.get("pid")
    cwd = session_data.get("cwd", "")
    alive = is_pid_alive(pid)

    if not alive:
        return {
            "sessionId": session_id,
            "pid": pid,
            "project": Path(cwd).name,
            "status": "dead",
            "alive": False,
        }

    # Analyze last entries
    jsonl = find_jsonl(session_id)
    status = "unknown"
    last_activity = ""
    waiting_question = ""
    stall_seconds = 0

    if jsonl:
        lines = read_last_n_lines(jsonl, 10)
        entries = parse_jsonl_entries(lines)

        if entries:
            # Skip trailing empty user messages (hook artifacts)
            last = entries[-1]
            if last["type"] == "user" and not last.get("text", "").strip():
                # Look at the entry before it instead
                for candidate in reversed(entries[:-1]):
                    if candidate.get("text", "").strip() or candidate["type"] != "user":
                        last = candidate
                        break

            last_activity = last.get("timestamp", "")

            # Calculate age of last entry
            age_seconds = _entry_age_seconds(last_activity)

            if last["type"] == "assistant" and last.get("stop_reason") == "end_turn":
                status = "waiting_for_input"
                waiting_question = last.get("text", "")[:500]
            elif last["type"] == "assistant" and last.get("stop_reason") == "tool_use" and age_seconds > STALL_THRESHOLD_SECONDS:
                # Tool was called but no result came back — stuck on approval
                status = "possibly_stuck_on_approval"
                stall_seconds = int(age_seconds)
            elif last["type"] in ("progress", "user") and not last.get("text", "").strip() and age_seconds > STALL_THRESHOLD_SECONDS:
                status = "possibly_stuck_on_approval"
                stall_seconds = int(age_seconds)
            elif last["type"] == "progress" and age_seconds > STALL_THRESHOLD_SECONDS:
                status = "possibly_stuck_on_approval"
                stall_seconds = int(age_seconds)
            elif last["type"] == "assistant" and last.get("tool_calls"):
                status = "processing"
            elif last["type"] == "user":
                status = "processing"
            elif last["type"] == "progress":
                status = "processing"
            else:
                status = "active"

    result = {
        "sessionId": session_id,
        "pid": pid,
        "project": Path(cwd).name,
        "cwd": cwd,
        "status": status,
        "alive": True,
        "lastActivity": last_activity,
        "waitingQuestion": waiting_question if status == "waiting_for_input" else "",
    }
    if stall_seconds > 0:
        result["stallSeconds"] = stall_seconds
    return result


def tool_get_all_waiting(args):
    waiting = []

    for sf in SESSIONS_DIR.glob("*.json"):
        try:
            with open(sf) as f:
                data = json.load(f)
        except Exception:
            continue

        pid = data.get("pid")
        session_id = data.get("sessionId", "")
        cwd = data.get("cwd", "")

        if not is_pid_alive(pid):
            continue

        jsonl = find_jsonl(session_id)
        if not jsonl:
            continue

        lines = read_last_n_lines(jsonl, 10)
        entries = parse_jsonl_entries(lines)

        if not entries:
            continue

        # Skip trailing empty user messages (hook artifacts)
        last = entries[-1]
        if last["type"] == "user" and not last.get("text", "").strip():
            for candidate in reversed(entries[:-1]):
                if candidate.get("text", "").strip() or candidate["type"] != "user":
                    last = candidate
                    break

        age_seconds = _entry_age_seconds(last.get("timestamp", ""))

        # Detect: assistant finished and waiting for user message
        is_waiting = (
            last["type"] == "assistant" and last.get("stop_reason") == "end_turn"
        )
        # Detect: no new meaningful events for >90s (likely approval prompt)
        is_stalled = (
            last["type"] == "assistant"
            and last.get("stop_reason") == "tool_use"
            and age_seconds > STALL_THRESHOLD_SECONDS
        ) or (
            last["type"] in ("progress", "user")
            and not last.get("text", "").strip()
            and age_seconds > STALL_THRESHOLD_SECONDS
        ) or (
            last["type"] == "progress" and age_seconds > STALL_THRESHOLD_SECONDS
        )

        if is_waiting or is_stalled:
            last_user = ""
            for e in reversed(entries):
                if e["type"] == "user":
                    last_user = e["text"][:200]
                    break

            reason = "waiting_for_input" if is_waiting else "possibly_stuck_on_approval"
            stall_info = int(age_seconds) if is_stalled else 0

            entry = {
                    "sessionId": session_id,
                    "pid": pid,
                    "project": Path(cwd).name,
                    "cwd": cwd,
                    "reason": reason,
                    "assistantMessage": last.get("text", "")[:500],
                    "lastUserMessage": last_user,
                    "timestamp": last.get("timestamp", ""),
                }
            if stall_info > 0:
                entry["stallSeconds"] = stall_info
            waiting.append(entry)

    return {"waiting": waiting, "count": len(waiting)}


# ---------------------------------------------------------------------------
# Phase 2 — Goal management, decisions, logging
# ---------------------------------------------------------------------------

def _read_json(path, default=None):
    """Read a JSON file, return default if missing or invalid."""
    if default is None:
        default = {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    """Atomically write JSON (write tmp, rename)."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def tool_set_goal(args):
    session_id = args["session_id"]
    goal = args["goal"]
    risk_level = args.get("risk_level", "medium")

    goals = _read_json(GOALS_FILE)
    goals[session_id] = {
        "goal": goal,
        "riskLevel": risk_level,
        "registeredAt": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(GOALS_FILE, goals)

    return {"status": "ok", "sessionId": session_id, "goal": goal, "riskLevel": risk_level}


def tool_get_goals(args):
    goals = _read_json(GOALS_FILE)
    return {"goals": goals, "count": len(goals)}


def tool_make_decision(args):
    session_id = args["session_id"]

    # Get goal
    goals = _read_json(GOALS_FILE)
    goal_info = goals.get(session_id, {})
    goal = goal_info.get("goal", "")
    risk_level = goal_info.get("riskLevel", "unknown")

    # Get current status
    status_result = tool_get_status({"session_id": session_id})
    status = status_result.get("status", "unknown")
    project = status_result.get("project", "unknown")

    # Get recent activity
    activity_result = tool_get_activity({"session_id": session_id, "last_n": 10})
    entries = activity_result.get("entries", [])

    # Find last tool call type
    last_tool = ""
    for e in reversed(entries):
        if e.get("tool_calls"):
            last_tool = ", ".join(e["tool_calls"])
            break

    # Find last meaningful assistant message
    last_assistant_msg = ""
    for e in reversed(entries):
        if e["type"] == "assistant" and e.get("text", "").strip():
            last_assistant_msg = e["text"][:300]
            break

    # Find last user message
    last_user_msg = ""
    for e in reversed(entries):
        if e["type"] == "user" and e.get("text", "").strip():
            last_user_msg = e["text"][:200]
            break

    # Decision logic
    recommendation = "escalate"
    reason = ""

    if status != "possibly_stuck_on_approval":
        recommendation = "no_action"
        reason = f"Session is '{status}', not stalled on approval."
    elif not goal:
        recommendation = "escalate"
        reason = "No goal registered for this session. Cannot assess if action is within scope."
    else:
        # Evaluate based on tool type + risk level
        tool_lower = last_tool.lower()

        # Safe patterns
        safe_tools = ["read", "glob", "grep", "webfetch", "websearch"]
        git_read = any(kw in last_assistant_msg.lower() for kw in [
            "git log", "git status", "git diff", "git branch", "worktree list",
            "git show", "git remote"
        ])
        git_write = any(kw in last_assistant_msg.lower() for kw in [
            "git push", "git reset", "force push", "branch -d", "branch -D",
            "git clean", "git checkout --"
        ])
        git_commit = any(kw in last_assistant_msg.lower() for kw in [
            "git commit", "git add"
        ])

        if any(t in tool_lower for t in safe_tools):
            recommendation = "approve"
            reason = f"Read-only tool ({last_tool}). Within any goal scope."
        elif "bash" in tool_lower and git_read:
            recommendation = "approve"
            reason = f"Read-only git command. Safe."
        elif "bash" in tool_lower and git_commit and risk_level in ("low", "medium"):
            recommendation = "approve"
            reason = f"Git commit within {risk_level}-risk session. Goal: {goal[:100]}"
        elif "bash" in tool_lower and git_write:
            recommendation = "escalate"
            reason = f"Destructive git operation in context: '{last_assistant_msg[:100]}'. Needs human approval."
        elif "bash" in tool_lower and risk_level == "low":
            recommendation = "approve"
            reason = f"Bash in low-risk session. Goal: {goal[:100]}"
        elif "bash" in tool_lower and risk_level == "high":
            recommendation = "escalate"
            reason = f"Bash in high-risk session. Goal: {goal[:100]}. Could be external API or destructive."
        elif "write" in tool_lower or "edit" in tool_lower:
            recommendation = "approve"
            reason = f"File write/edit. Normal development operation. Goal: {goal[:100]}"
        else:
            recommendation = "escalate"
            reason = f"Unknown tool pattern ({last_tool}) in {risk_level}-risk session. Needs review."

    # Log the decision
    decisions = _read_json(DECISIONS_FILE, default=[])
    decision_entry = {
        "sessionId": session_id,
        "project": project,
        "recommendation": recommendation,
        "reason": reason,
        "lastTool": last_tool,
        "lastAssistantMessage": last_assistant_msg[:200],
        "lastUserMessage": last_user_msg[:200],
        "goal": goal,
        "riskLevel": risk_level,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    decisions.append(decision_entry)
    # Keep last 100 decisions
    if len(decisions) > 100:
        decisions = decisions[-100:]
    _write_json(DECISIONS_FILE, decisions)

    return decision_entry


def tool_log_event(args):
    event = args["event"]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Create log file if it doesn't exist
    if not LOG_FILE.exists():
        LOG_FILE.write_text("# Conductor Activity Log\n\n")

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"- [{timestamp}] {event}\n")

    return {"status": "ok", "logged": event}


# ---------------------------------------------------------------------------
# Phase 3 — Flag management
# ---------------------------------------------------------------------------

FLAGS_DIR = CONDUCTOR_DIR / "flags"


def tool_set_flag(args):
    session_id = args["session_id"]
    action = args["action"]
    reason = args.get("reason", "")
    tool = args.get("tool", "")

    FLAGS_DIR.mkdir(parents=True, exist_ok=True)

    flag = {
        "action": action,
        "reason": reason,
        "setAt": datetime.now(timezone.utc).isoformat(),
    }
    if action == "block_tool" and tool:
        flag["tool"] = tool

    flag_file = FLAGS_DIR / f"{session_id}.json"
    _write_json(flag_file, flag)

    return {"status": "ok", "sessionId": session_id, "action": action, "reason": reason}


def tool_clear_flag(args):
    session_id = args["session_id"]
    flag_file = FLAGS_DIR / f"{session_id}.json"

    if flag_file.exists():
        flag_file.unlink()
        return {"status": "ok", "sessionId": session_id, "cleared": True}

    return {"status": "ok", "sessionId": session_id, "cleared": False, "note": "No flag was set"}


def tool_get_flags(args):
    FLAGS_DIR.mkdir(parents=True, exist_ok=True)
    flags = {}

    for flag_file in FLAGS_DIR.glob("*.json"):
        session_id = flag_file.stem
        try:
            flags[session_id] = _read_json(flag_file)
        except Exception:
            continue

    return {"flags": flags, "count": len(flags)}


# ---------------------------------------------------------------------------
# Phase 4 — RC session discovery + task sending
# ---------------------------------------------------------------------------

def tool_discover_rc_sessions(args):
    """Find all Remote Control sessions from bridge-pointer.json files and API."""
    sessions = []

    # Local discovery via bridge-pointer.json
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        pointer = project_dir / "bridge-pointer.json"
        if not pointer.exists():
            continue
        try:
            with open(pointer) as f:
                data = json.load(f)
            session_id = data.get("sessionId", "")
            if not session_id:
                continue

            # Friendly name from dir: e.g. C--Users-Jane-Desktop-myproject → myproject
            parts = project_dir.name.split("-")
            project_name = parts[-1] if parts else project_dir.name

            sessions.append({
                "sessionId": session_id,
                "project": project_name,
                "environmentId": data.get("environmentId", ""),
                "source": "bridge-pointer",
            })
        except Exception:
            continue

    # API discovery
    try:
        creds_file = CLAUDE_HOME / ".credentials.json"
        claude_json = Path.home() / ".claude.json"
        if creds_file.exists() and claude_json.exists():
            creds = _read_json(creds_file)
            config = _read_json(claude_json)
            token = creds.get("claudeAiOauth", {}).get("accessToken", "")
            org = config.get("oauthAccount", {}).get("organizationUuid", "")

            if token and org:
                import urllib.request
                import urllib.error

                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/code/sessions",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-version": "2023-06-01",
                        "x-organization-uuid": org,
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        api_data = json.loads(resp.read())
                    api_sessions = {
                        s["id"].replace("cse_", "session_"): s
                        for s in api_data.get("data", [])
                    }
                    # Enrich local sessions with API status
                    local_ids = {s["sessionId"] for s in sessions}
                    for s in sessions:
                        api_s = api_sessions.get(s["sessionId"])
                        if api_s:
                            s["connectionStatus"] = api_s.get("connection_status", "")
                            s["createdAt"] = api_s.get("created_at", "")

                    # Add API-only sessions
                    for sid, api_s in api_sessions.items():
                        if sid not in local_ids:
                            sessions.append({
                                "sessionId": sid,
                                "project": api_s.get("title", "unknown"),
                                "connectionStatus": api_s.get("connection_status", ""),
                                "createdAt": api_s.get("created_at", ""),
                                "source": "api",
                            })
                except (urllib.error.URLError, Exception):
                    pass
    except Exception:
        pass

    return {"sessions": sessions, "count": len(sessions)}


def tool_send_task(args):
    """Send a message/task to a Remote Control session via Anthropic API."""
    session_id = args["session_id"]
    message = args["message"]

    try:
        creds_file = CLAUDE_HOME / ".credentials.json"
        claude_json = Path.home() / ".claude.json"
        creds = _read_json(creds_file)
        config = _read_json(claude_json)
        token = creds.get("claudeAiOauth", {}).get("accessToken", "")
        org = config.get("oauthAccount", {}).get("organizationUuid", "")

        if not token:
            return {"error": "No OAuth token found"}

        import urllib.request
        import uuid as uuid_mod

        payload = json.dumps({
            "events": [{
                "uuid": str(uuid_mod.uuid4()),
                "type": "user",
                "message": {
                    "role": "user",
                    "content": message,
                },
            }]
        }).encode()

        req = urllib.request.Request(
            f"https://api.anthropic.com/v1/sessions/{session_id}/events",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "ccr-byoc-2025-07-29",
                "x-organization-uuid": org,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = resp.read().decode()

        return {
            "status": "sent" if status in (200, 201) else "failed",
            "httpStatus": status,
            "sessionId": session_id,
            "message": message[:100],
        }
    except Exception as e:
        return {"error": str(e), "sessionId": session_id}


# ---------------------------------------------------------------------------
# MCP protocol handler
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "list_sessions": tool_list_sessions,
    "get_activity": tool_get_activity,
    "get_status": tool_get_status,
    "get_all_waiting": tool_get_all_waiting,
    "set_goal": tool_set_goal,
    "get_goals": tool_get_goals,
    "make_decision": tool_make_decision,
    "log_event": tool_log_event,
    "set_flag": tool_set_flag,
    "clear_flag": tool_clear_flag,
    "get_flags": tool_get_flags,
    "discover_rc_sessions": tool_discover_rc_sessions,
    "send_task": tool_send_task,
}


def handle_request(request):
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    # --- initialize ---
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "claude-bridge", "version": "1.0.0"},
            },
        }

    # --- notifications (no response) ---
    if method.startswith("notifications/"):
        return None

    # --- tools/list ---
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    # --- tools/call ---
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        try:
            result = handler(tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2)}
                    ]
                },
            }
        except Exception as e:
            log(f"Tool error ({tool_name}): {e}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    # --- ping ---
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    # Unknown method with id → error; without id → ignore
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    log("starting...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"Invalid JSON: {e}")
            continue

        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

    log("stdin closed, shutting down.")


if __name__ == "__main__":
    main()
