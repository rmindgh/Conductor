#!/usr/bin/env python3
"""
Claude Conductor — Remote Control Client (Phase 4)

Connects to Claude Code's sessions API via WebSocket to:
  - Receive tool permission requests in real-time
  - Auto-approve safe operations based on goals + risk level
  - Escalate dangerous operations (log + Telegram)
  - Send messages to sessions

Uses the OAuth token from ~/.claude/.credentials.json (plaintext on Windows/Linux).

Usage:
  python remote-control.py                    # Monitor all active sessions
  python remote-control.py --session ID       # Monitor specific session
  python remote-control.py --approve-all      # Auto-approve everything (dangerous!)
  python remote-control.py --dry-run          # Log decisions without acting
"""

import asyncio
import json
import sys
import os
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import websockets
import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLAUDE_HOME = Path.home() / ".claude"
CREDENTIALS_FILE = CLAUDE_HOME / ".credentials.json"
SESSIONS_DIR = CLAUDE_HOME / "sessions"
CONDUCTOR_DIR = CLAUDE_HOME / "conductor"
GOALS_FILE = CONDUCTOR_DIR / "goals.json"
LOG_FILE = CONDUCTOR_DIR / "log.md"
DECISIONS_FILE = CONDUCTOR_DIR / "decisions.json"

API_BASE = "https://api.anthropic.com/v1"
WS_BASE = "wss://api.anthropic.com/v1"

# Dangerous patterns — always escalate, never auto-approve
DANGEROUS_PATTERNS = [
    "git push --force", "git push -f",
    "git reset --hard", "git clean -f",
    "rm -rf /", "rm -rf ~", "rm -rf *",
    "DROP TABLE", "DROP DATABASE",
    "format c:", "del /s",
]

# Safe tool names — always approve
SAFE_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("conductor-rc")


def log_to_file(event: str):
    """Append event to conductor log.md"""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"- [{ts}] {event}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_oauth_token() -> dict:
    """Read OAuth credentials from .credentials.json"""
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(f"No credentials at {CREDENTIALS_FILE}")

    with open(CREDENTIALS_FILE) as f:
        data = json.load(f)

    oauth = data.get("claudeAiOauth", {})
    if not oauth.get("accessToken"):
        raise ValueError("No accessToken in credentials")

    # Check expiry
    expires_at = oauth.get("expiresAt", 0)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if now_ms > expires_at:
        log.warning("OAuth token appears expired. May need to refresh Claude Code.")

    return oauth


def get_org_uuid() -> str:
    """Read org UUID from .claude.json oauthAccount"""
    try:
        claude_json = CLAUDE_HOME.parent / ".claude.json"
        # Handle Windows home directory
        if not claude_json.exists():
            claude_json = Path.home() / ".claude.json"
        with open(claude_json) as f:
            data = json.load(f)
        return data.get("oauthAccount", {}).get("organizationUuid", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Goals & Decision Logic
# ---------------------------------------------------------------------------

def read_goals() -> dict:
    try:
        with open(GOALS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def make_decision(tool_name: str, tool_input: dict, session_id: str, goals: dict) -> tuple[str, str]:
    """
    Decide whether to approve or escalate a tool permission request.
    Returns: (decision, reason) where decision is "approve" or "escalate"
    """
    goal_info = goals.get(session_id, {})
    goal = goal_info.get("goal", "")
    risk_level = goal_info.get("riskLevel", "unknown")

    # Always approve safe tools
    if tool_name in SAFE_TOOLS:
        return "approve", f"Safe tool ({tool_name})"

    # Check for dangerous patterns in Bash commands
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        for pattern in DANGEROUS_PATTERNS:
            if pattern.lower() in command.lower():
                return "escalate", f"Dangerous: '{command[:80]}' matches '{pattern}'"

        # Git read commands — safe
        git_read_cmds = ["git log", "git status", "git diff", "git branch", "git show", "git remote", "worktree list"]
        if any(cmd in command.lower() for cmd in git_read_cmds):
            return "approve", f"Read-only git command"

        # Git commit in low/medium risk
        if any(cmd in command.lower() for cmd in ["git commit", "git add"]) and risk_level in ("low", "medium"):
            return "approve", f"Git commit in {risk_level}-risk session"

        # Git push (non-force) in low/medium risk
        if "git push" in command.lower() and "--force" not in command.lower() and "-f" not in command.split():
            if risk_level in ("low", "medium"):
                return "approve", f"Git push (non-force) in {risk_level}-risk session"
            else:
                return "escalate", f"Git push in {risk_level}-risk session"

        # General Bash in low risk
        if risk_level == "low":
            return "approve", f"Bash in low-risk session. Goal: {goal[:80]}"

        # General Bash in high risk
        if risk_level == "high":
            return "escalate", f"Bash in high-risk session. Goal: {goal[:80]}"

        # Medium risk — approve unless no goal
        if goal:
            return "approve", f"Bash in medium-risk session. Goal: {goal[:80]}"
        else:
            return "escalate", f"Bash with no registered goal"

    # Write/Edit — normally safe
    if tool_name in ("Write", "Edit", "MultiEdit"):
        return "approve", f"File operation ({tool_name})"

    # Agent — approve for low/medium, escalate for high
    if tool_name == "Agent":
        if risk_level in ("low", "medium"):
            return "approve", f"Agent in {risk_level}-risk session"
        return "escalate", f"Agent in {risk_level}-risk session"

    # No goal registered — escalate unknown tools
    if not goal:
        return "escalate", f"Unknown tool ({tool_name}), no goal registered"

    # Default: approve if goal exists
    return "approve", f"Tool {tool_name} with goal: {goal[:80]}"


def log_decision(session_id: str, tool_name: str, decision: str, reason: str, project: str = ""):
    """Log decision to decisions.json"""
    try:
        decisions = []
        if DECISIONS_FILE.exists():
            with open(DECISIONS_FILE) as f:
                decisions = json.load(f)

        decisions.append({
            "sessionId": session_id,
            "project": project,
            "recommendation": decision,
            "reason": reason,
            "lastTool": tool_name,
            "source": "remote-control",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Cap at 100
        if len(decisions) > 100:
            decisions = decisions[-100:]

        with open(DECISIONS_FILE, "w") as f:
            json.dump(decisions, f, indent=2)
    except Exception as e:
        log.error(f"Failed to log decision: {e}")


# ---------------------------------------------------------------------------
# Session Discovery — Auto-discover --rc sessions
# ---------------------------------------------------------------------------

def discover_rc_sessions_local() -> list[dict]:
    """
    Find --rc sessions by reading bridge-pointer.json files across all project dirs.
    Returns list of {session_id, project, cwd, environment_id}.
    """
    sessions = []
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
            env_id = data.get("environmentId", "")
            if not session_id:
                continue

            # Derive friendly project name from directory
            # e.g. C--Users-Jane-Desktop-myproject → myproject
            dir_name = project_dir.name
            parts = dir_name.split("-")
            project_name = parts[-1] if parts else dir_name

            sessions.append({
                "sessionId": session_id,
                "environmentId": env_id,
                "project": project_name,
                "projectDir": str(project_dir),
                "source": "bridge-pointer",
            })
        except Exception:
            continue
    return sessions


def discover_rc_sessions_api(access_token: str, org_uuid: str) -> list[dict]:
    """
    Find active --rc sessions via the Anthropic API.
    Returns list of {session_id, project, connection_status, created_at}.
    """
    sessions = []
    try:
        resp = httpx.get(
            f"{API_BASE}/code/sessions",
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-version": "2023-06-01",
                "x-organization-uuid": org_uuid,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return sessions

        for s in resp.json().get("data", []):
            cse_id = s.get("id", "")
            status = s.get("connection_status", "")
            # Convert cse_ to session_ format
            session_id = cse_id.replace("cse_", "session_")
            title = s.get("title", "")

            sessions.append({
                "sessionId": session_id,
                "cseId": cse_id,
                "connectionStatus": status,
                "project": title or "unknown",
                "createdAt": s.get("created_at", ""),
                "source": "api",
            })
    except Exception as e:
        log.warning(f"API session discovery failed: {e}")

    return sessions


def discover_all_rc_sessions(access_token: str, org_uuid: str) -> list[dict]:
    """
    Merge local bridge-pointer discovery with API discovery.
    Prefer API data (has connection status), enrich with local data (has project name).
    Only return sessions that are connected or recently active.
    """
    local = {s["sessionId"]: s for s in discover_rc_sessions_local()}
    api = {s["sessionId"]: s for s in discover_rc_sessions_api(access_token, org_uuid)}

    merged = {}
    for sid, data in api.items():
        entry = data.copy()
        # Enrich with local project name if available
        if sid in local:
            local_data = local[sid]
            if local_data.get("project") and local_data["project"] != "unknown":
                entry["project"] = local_data["project"]
            entry["projectDir"] = local_data.get("projectDir", "")
        merged[sid] = entry

    # Add local-only sessions (bridge-pointer exists but not in API — might be stale)
    for sid, data in local.items():
        if sid not in merged:
            data["connectionStatus"] = "unknown"
            merged[sid] = data

    # Filter: only connected or unknown (stale pointers might still be active)
    active = [
        s for s in merged.values()
        if s.get("connectionStatus") in ("connected", "unknown", "")
    ]

    return active


def session_friendly_name(session: dict) -> str:
    """Get a human-readable name for a session."""
    project = session.get("project", "")
    if project and project != "unknown":
        return project
    # Fall back to session ID prefix
    sid = session.get("sessionId", "")
    return sid[:12] if sid else "unnamed"


# ---------------------------------------------------------------------------
# WebSocket Session Monitor
# ---------------------------------------------------------------------------

class SessionMonitor:
    """Monitors a single Claude Code session via WebSocket"""

    def __init__(
        self,
        session_id: str,
        access_token: str,
        org_uuid: str,
        goals: dict,
        project: str = "",
        dry_run: bool = False,
        approve_all: bool = False,
    ):
        self.session_id = session_id
        self.access_token = access_token
        self.org_uuid = org_uuid
        self.goals = goals
        self.project = project
        self.dry_run = dry_run
        self.approve_all = approve_all
        self.ws = None
        self.connected = False
        self.short_id = session_id[:8]

    async def connect(self):
        """Connect to session WebSocket and handle permission requests"""
        url = f"{WS_BASE}/sessions/ws/{self.session_id}/subscribe"
        if self.org_uuid:
            url += f"?organization_uuid={self.org_uuid}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "anthropic-version": "2023-06-01",
        }

        retry_count = 0
        max_retries = 5

        while retry_count < max_retries:
            try:
                log.info(f"[{self.short_id}] Connecting to {self.project}...")
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    retry_count = 0
                    log.info(f"[{self.short_id}] Connected to {self.project}")
                    log_to_file(f"RC connected to {self.project} ({self.short_id})")

                    async for message in ws:
                        log.debug(f"[{self.short_id}] RAW: {str(message)[:200]}")
                        try:
                            parsed = json.loads(message)
                            await self.handle_message(parsed)
                        except json.JSONDecodeError:
                            log.warning(f"[{self.short_id}] Non-JSON message: {str(message)[:100]}")

            except websockets.exceptions.ConnectionClosedError as e:
                if e.code == 4003:
                    log.error(f"[{self.short_id}] Unauthorized (4003). Token may be invalid.")
                    break
                elif e.code == 4001:
                    retry_count += 1
                    log.warning(f"[{self.short_id}] Session not found (4001). Retry {retry_count}/{max_retries}")
                else:
                    retry_count += 1
                    log.warning(f"[{self.short_id}] Connection closed ({e.code}). Retry {retry_count}/{max_retries}")

            except Exception as e:
                retry_count += 1
                log.warning(f"[{self.short_id}] Error: {e}. Retry {retry_count}/{max_retries}")

            if retry_count < max_retries:
                await asyncio.sleep(2 * retry_count)

        self.connected = False
        log.info(f"[{self.short_id}] Disconnected from {self.project}")

    async def handle_message(self, msg: dict):
        """Handle incoming WebSocket messages"""
        msg_type = msg.get("type", "")

        if msg_type == "control_request":
            request_id = msg.get("request_id", "")
            request = msg.get("request", {})
            subtype = request.get("subtype", "")

            if subtype == "initialize":
                # Must respond to initialize or server kills connection in ~10s
                log.info(f"[{self.short_id}] Initialize request — responding")
                response = {
                    "type": "control_response",
                    "session_id": self.session_id,
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {
                            "commands": [],
                            "output_style": "normal",
                            "available_output_styles": ["normal"],
                            "models": [],
                            "account": {},
                            "pid": os.getpid(),
                        },
                    },
                }
                await self.ws.send(json.dumps(response))

            elif subtype == "set_model":
                # Acknowledge model change
                log.info(f"[{self.short_id}] Set model request — acknowledging")
                response = {
                    "type": "control_response",
                    "session_id": self.session_id,
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {},
                    },
                }
                await self.ws.send(json.dumps(response))

            elif subtype == "set_permission_mode":
                # Acknowledge permission mode change
                log.info(f"[{self.short_id}] Set permission mode — acknowledging")
                response = {
                    "type": "control_response",
                    "session_id": self.session_id,
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {},
                    },
                }
                await self.ws.send(json.dumps(response))

            elif subtype == "can_use_tool":
                tool_name = request.get("tool_name", "")
                tool_input = request.get("input", {})
                display_name = request.get("display_name", tool_name)

                log.info(f"[{self.short_id}] Permission request: {display_name}")

                # Make decision
                if self.approve_all:
                    decision, reason = "approve", "approve_all mode"
                else:
                    decision, reason = make_decision(
                        tool_name, tool_input, self.session_id, self.goals
                    )

                log.info(f"[{self.short_id}] Decision: {decision} — {reason}")
                log_decision(self.session_id, tool_name, decision, reason, self.project)

                if self.dry_run:
                    log.info(f"[{self.short_id}] DRY RUN — not sending response")
                    log_to_file(f"RC DRY RUN {self.project}: {decision} {tool_name} — {reason}")
                    return

                if decision == "approve":
                    await self.approve(request_id)
                    log_to_file(f"RC APPROVED {self.project}: {tool_name} — {reason}")
                else:
                    # Don't deny — just don't respond. Let it sit for human.
                    log.warning(f"[{self.short_id}] ESCALATED — not auto-approving {tool_name}")
                    log_to_file(f"RC ESCALATED {self.project}: {tool_name} — {reason}")

            elif subtype == "interrupt":
                log.info(f"[{self.short_id}] Interrupt request received")

            else:
                # Unknown subtype — acknowledge to prevent timeout
                log.info(f"[{self.short_id}] Unknown control request: {subtype} — acknowledging")
                response = {
                    "type": "control_response",
                    "session_id": self.session_id,
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {},
                    },
                }
                await self.ws.send(json.dumps(response))

        elif msg_type == "control_cancel_request":
            request_id = msg.get("request_id", "")
            log.info(f"[{self.short_id}] Request cancelled: {request_id[:8]}")

    async def approve(self, request_id: str):
        """Send approval response"""
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "allow",
                    "updatedInput": {},
                },
            },
        }
        await self.ws.send(json.dumps(response))
        log.info(f"[{self.short_id}] Approved (request {request_id[:8]})")

    async def deny(self, request_id: str, message: str = "Denied by conductor"):
        """Send denial response"""
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "deny",
                    "message": message,
                },
            },
        }
        await self.ws.send(json.dumps(response))
        log.info(f"[{self.short_id}] Denied (request {request_id[:8]}): {message}")

    async def send_message(self, content: str):
        """Send a user message to the session via HTTP"""
        import uuid as uuid_mod
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{API_BASE}/sessions/{self.session_id}/events",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "ccr-byoc-2025-07-29",
                },
                json={
                    "events": [
                        {
                            "uuid": str(uuid_mod.uuid4()),
                            "session_id": self.session_id,
                            "type": "user",
                            "parent_tool_use_id": None,
                            "message": {
                                "role": "user",
                                "content": content,
                            },
                        }
                    ]
                },
                timeout=10,
            )
            return resp.status_code in (200, 201)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args):
    """Main async entry point — discovers and monitors RC sessions continuously"""
    # Load credentials
    oauth = get_oauth_token()
    access_token = oauth["accessToken"]
    log.info(f"Token loaded (expires: {datetime.fromtimestamp(oauth['expiresAt']/1000).isoformat()})")

    # Get org UUID
    org_uuid = get_org_uuid()
    if org_uuid:
        log.info(f"Org UUID: {org_uuid[:8]}...")
    else:
        log.warning("Could not determine org UUID — connecting without it")

    # Load goals
    goals = read_goals()
    log.info(f"Loaded {len(goals)} session goals")

    # Track active monitors by session ID
    active_monitors: dict[str, asyncio.Task] = {}

    async def scan_and_connect():
        """Discover RC sessions and connect to new ones."""
        if args.session:
            sessions = [{"sessionId": args.session, "project": "manual"}]
        else:
            sessions = discover_all_rc_sessions(access_token, org_uuid)

        new_count = 0
        for sess in sessions:
            sid = sess.get("sessionId", "")
            if not sid or sid in active_monitors:
                continue

            name = session_friendly_name(sess)
            log.info(f"New RC session: {name} ({sid[:16]}...)")

            monitor = SessionMonitor(
                session_id=sid,
                access_token=access_token,
                org_uuid=org_uuid,
                goals=goals,
                project=name,
                dry_run=args.dry_run,
                approve_all=args.approve_all,
            )
            task = asyncio.create_task(monitor.connect())
            active_monitors[sid] = task
            new_count += 1

        if new_count:
            log.info(f"Connected {new_count} new session(s). Total active: {len(active_monitors)}")
            log_to_file(f"RC scan: {new_count} new, {len(active_monitors)} total")

        # Clean up finished tasks (disconnected sessions)
        dead = [sid for sid, task in active_monitors.items() if task.done()]
        for sid in dead:
            del active_monitors[sid]
            log.info(f"Session {sid[:12]} disconnected, removed from monitors")

    # Initial scan
    await scan_and_connect()

    if not active_monitors and not args.session:
        log.warning("No RC sessions found. Start sessions with 'claude --rc'")
        log.info("Scanning every 30s for new RC sessions...")

    # Continuous scan loop — check for new sessions every 30s
    scan_interval = 30
    try:
        while True:
            await asyncio.sleep(scan_interval)

            # Refresh credentials if needed
            try:
                oauth = get_oauth_token()
                access_token = oauth["accessToken"]
            except Exception:
                pass

            # Refresh goals
            goals = read_goals()

            # Scan for new sessions
            await scan_and_connect()

            # Status report
            alive = sum(1 for t in active_monitors.values() if not t.done())
            if alive:
                log.debug(f"Status: {alive} active monitors")

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down...")
        for task in active_monitors.values():
            task.cancel()
        await asyncio.gather(*active_monitors.values(), return_exceptions=True)
        log.info("All monitors stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Claude Conductor — Remote Control Service",
        epilog="Runs continuously, auto-discovers --rc sessions, approves safe tools.",
    )
    parser.add_argument("--session", help="Monitor specific session ID (skip auto-discovery)")
    parser.add_argument("--approve-all", action="store_true", help="Auto-approve everything (dangerous!)")
    parser.add_argument("--dry-run", action="store_true", help="Log decisions without acting")
    parser.add_argument("--scan-interval", type=int, default=30, help="Seconds between session scans (default: 30)")
    args = parser.parse_args()

    if args.approve_all and not args.dry_run:
        log.warning("APPROVE-ALL mode — every tool call will be auto-approved!")
        log.warning("Press Ctrl+C within 3 seconds to abort...")
        try:
            import time
            time.sleep(3)
        except KeyboardInterrupt:
            log.info("Aborted.")
            return

    log.info("Claude Conductor Remote Control starting...")
    log.info("Sessions with 'claude --rc' will be auto-discovered and monitored.")
    log.info("Press Ctrl+C to stop.")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
