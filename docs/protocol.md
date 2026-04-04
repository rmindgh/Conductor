# Remote Control API Protocol

Claude Code sessions started with `claude --rc` register with Anthropic's API and can be controlled programmatically. This document describes the protocol as discovered through testing.

**Disclaimer:** This is an undocumented API. It may change between Claude Code versions without notice.

## Authentication

OAuth tokens are stored locally by Claude Code:

| Platform | Location | Format |
|----------|----------|--------|
| Windows | `~/.claude/.credentials.json` | Plaintext JSON |
| Linux | `~/.claude/.credentials.json` | Plaintext JSON |
| macOS | macOS Keychain | Encrypted |

Token structure:
```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat01-...",
    "refreshToken": "sk-ant-ort01-...",
    "expiresAt": 1775309715204,
    "scopes": ["user:sessions:claude_code", "..."]
  }
}
```

Organization UUID is in `~/.claude.json`:
```json
{
  "oauthAccount": {
    "organizationUuid": "your-org-uuid"
  }
}
```

## Session Discovery

### Local: bridge-pointer.json

When a session starts with `--rc`, Claude Code writes a pointer file:
```
~/.claude/projects/{project-dir}/bridge-pointer.json
```

Contents:
```json
{
  "sessionId": "session_01ABC...",
  "environmentId": "env_01XYZ...",
  "source": "repl"
}
```

The `sessionId` is the server-side ID needed for WebSocket and HTTP APIs.

### API: List sessions

```
GET https://api.anthropic.com/v1/code/sessions
Headers:
  Authorization: Bearer {oauth_token}
  anthropic-version: 2023-06-01
  x-organization-uuid: {org_uuid}
```

Returns sessions with `connection_status` ("connected" / "disconnected").

## WebSocket Subscription

### Connect

```
wss://api.anthropic.com/v1/sessions/ws/{session_id}/subscribe?organization_uuid={org_uuid}

Headers:
  Authorization: Bearer {oauth_token}
  anthropic-version: 2023-06-01
```

The `session_id` must be in `session_*` format (from bridge-pointer.json or API).

### Initialize handshake

After connecting, the server sends a `control_request` with `subtype: "initialize"`. **You must respond within ~10 seconds or the connection is killed** (close code 1006, no frame).

Server sends:
```json
{
  "type": "control_request",
  "request_id": "uuid",
  "request": {
    "subtype": "initialize"
  }
}
```

Client responds:
```json
{
  "type": "control_response",
  "session_id": "session_...",
  "response": {
    "subtype": "success",
    "request_id": "same-uuid",
    "response": {
      "commands": [],
      "output_style": "normal",
      "available_output_styles": ["normal"],
      "models": [],
      "account": {},
      "pid": 12345
    }
  }
}
```

### Tool approval flow

When a session needs tool approval, the server sends:
```json
{
  "type": "control_request",
  "request_id": "uuid",
  "request": {
    "subtype": "can_use_tool",
    "tool_name": "Bash",
    "tool_use_id": "toolu_...",
    "input": {"command": "git commit -m 'fix'"},
    "display_name": "Bash"
  }
}
```

To approve:
```json
{
  "type": "control_response",
  "session_id": "session_...",
  "response": {
    "subtype": "success",
    "request_id": "same-uuid",
    "response": {
      "behavior": "allow",
      "updatedInput": {}
    }
  }
}
```

To deny:
```json
{
  "type": "control_response",
  "session_id": "session_...",
  "response": {
    "subtype": "success",
    "request_id": "same-uuid",
    "response": {
      "behavior": "deny",
      "message": "Reason for denial"
    }
  }
}
```

### Conversation stream

The WebSocket also receives the full conversation — user messages, assistant responses, tool results. These arrive as message events alongside control requests.

### Other control requests

| Subtype | Purpose | Recommended response |
|---------|---------|---------------------|
| `initialize` | Connection setup (mandatory) | Full response with commands, models, etc. |
| `set_model` | Model change | Acknowledge with empty response |
| `set_permission_mode` | Permission mode change | Acknowledge with empty response |
| `interrupt` | Interrupt current work | Handle as needed |

Always respond to unknown subtypes with a generic success to avoid timeout disconnects.

## Sending Messages

Inject a user message into a session:

```
POST https://api.anthropic.com/v1/sessions/{session_id}/events
Headers:
  Authorization: Bearer {oauth_token}
  Content-Type: application/json
  anthropic-version: 2023-06-01
  anthropic-beta: ccr-byoc-2025-07-29
  x-organization-uuid: {org_uuid}

Body:
{
  "events": [{
    "uuid": "random-uuid",
    "type": "user",
    "message": {
      "role": "user",
      "content": "Your message here"
    }
  }]
}
```

The message appears in the target session as if the user typed it.

## Environment Registration

To create sessions programmatically (not via `claude --rc`):

### Step 1: Register environment
```
POST https://api.anthropic.com/v1/environments/bridge
Headers:
  anthropic-beta: environments-2025-11-01  (required!)

Body:
{
  "machine_name": "my-machine",
  "directory": "/path/to/project",
  "branch": "main",
  "max_sessions": 4,
  "metadata": {"worker_type": "repl"}
}
```

### Step 2: Create session
```
POST https://api.anthropic.com/v1/sessions
Headers:
  anthropic-beta: ccr-byoc-2025-07-29

Body:
{
  "environment_id": "env_...",
  "session_context": {"sources": [], "outcomes": []}
}
```

### Alternative: Code session (simpler)
```
POST https://api.anthropic.com/v1/code/sessions
Body:
{
  "title": "My Session",
  "bridge": {}
}
```

The `bridge: {}` field is required — without it the API returns 400.

## Beta Headers

| Header | Value | Required for |
|--------|-------|-------------|
| `anthropic-beta` | `environments-2025-11-01` | Environment registration |
| `anthropic-beta` | `ccr-byoc-2025-07-29` | Session events (send messages) |

## Common Issues

| Problem | Cause | Fix |
|---------|-------|-----|
| WebSocket disconnects after ~5s | No response to `initialize` | Handle `initialize` control_request immediately |
| WebSocket disconnects after ~5s | Wrong session ID format | Use `session_*` from bridge-pointer.json, not local UUIDs |
| 400 on session creation | Missing `bridge: {}` | Add `bridge: {}` to request body |
| 404 on environment registration | Missing beta header | Add `anthropic-beta: environments-2025-11-01` |
| Tool approval not received | Session not started with `--rc` | Restart with `claude --rc` |
| Token expired | OAuth token has TTL | Read fresh from `.credentials.json` (auto-refreshed by Claude Code) |
