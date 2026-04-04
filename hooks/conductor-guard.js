#!/usr/bin/env node
/**
 * Conductor Guard — PreToolUse hook
 *
 * Phase 3 of Claude Conductor. Runs in ALL sessions.
 *
 * Two-layer protection:
 *   1. Pattern-based: always blocks destructive commands (force push, rm -rf, etc.)
 *   2. Flag-based: conductor can set per-session block/proceed flags
 *
 * If no flag file exists and command isn't dangerous → tool proceeds (exit 0).
 * If flag says block → tool is blocked (exit 1).
 * If command matches dangerous pattern → tool is blocked (exit 1).
 *
 * This enables sessions to run with broad auto-approve permissions
 * while the conductor acts as a safety net.
 */

const fs = require('fs');
const path = require('path');

const CONDUCTOR_DIR = path.join(require('os').homedir(), '.claude', 'conductor');
const FLAGS_DIR = path.join(CONDUCTOR_DIR, 'flags');
const LOG_FILE = path.join(CONDUCTOR_DIR, 'log.md');

// Commands that should ALWAYS be blocked regardless of flags
const DANGEROUS_PATTERNS = [
  /git\s+push\s+.*--force/i,
  /git\s+push\s+-f\b/i,
  /git\s+reset\s+--hard/i,
  /git\s+clean\s+-f/i,
  /git\s+checkout\s+--\s+\./i,
  /\brm\s+-rf\s+[\/~]/i,          // rm -rf starting from root or home
  /\brm\s+-rf\s+\*/i,             // rm -rf *
  /DROP\s+(?:TABLE|DATABASE)/i,   // SQL destructive
  /DELETE\s+FROM\s+\w+\s*;/i,     // SQL delete without WHERE
  /format\s+[a-z]:/i,             // Windows format drive
  /del\s+\/[sfq]/i,               // Windows recursive delete
];

// Commands that are always safe — skip flag check for speed
const SAFE_TOOL_NAMES = ['Read', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'TaskCreate', 'TaskUpdate', 'TaskList', 'TaskGet'];

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  try {
    const data = JSON.parse(input);
    const toolName = data.tool_name || '';
    const toolInput = data.tool_input || {};
    const sessionId = data.session_id || '';

    // Always-safe tools — skip all checks
    if (SAFE_TOOL_NAMES.includes(toolName)) {
      process.exit(0);
    }

    // --- Layer 1: Pattern-based blocking ---
    if (toolName === 'Bash') {
      const command = toolInput.command || '';
      for (const pattern of DANGEROUS_PATTERNS) {
        if (pattern.test(command)) {
          logBlock(sessionId, toolName, command, `dangerous pattern: ${pattern.source}`);
          process.stdout.write(JSON.stringify({
            decision: 'block',
            reason: `CONDUCTOR: "${command.substring(0, 100)}" blocked — matches dangerous pattern: ${pattern.source}`,
          }));
          return process.exit(0);
        }
      }
    }

    // --- Layer 2: Flag-based control ---
    if (sessionId) {
      const flagFile = path.join(FLAGS_DIR, `${sessionId}.json`);
      try {
        if (fs.existsSync(flagFile)) {
          const flag = JSON.parse(fs.readFileSync(flagFile, 'utf8'));

          if (flag.action === 'block') {
            const reason = flag.reason || 'Conductor has paused this session.';
            logBlock(sessionId, toolName, toolInput.command || toolName, `flag: ${reason}`);
            process.stdout.write(JSON.stringify({ decision: 'block', reason: `CONDUCTOR: ${reason}` }));
            return process.exit(0);
          }

          if (flag.action === 'block_tool' && flag.tool === toolName) {
            const reason = flag.reason || `Tool ${toolName} blocked by conductor.`;
            logBlock(sessionId, toolName, toolInput.command || toolName, `flag: ${reason}`);
            process.stdout.write(JSON.stringify({ decision: 'block', reason: `CONDUCTOR: ${reason}` }));
            return process.exit(0);
          }

          // flag.action === 'proceed' or anything else → allow
        }
      } catch {
        // Flag file read error — fail open (allow)
      }
    }

    // No dangerous pattern, no block flag → allow
    process.exit(0);

  } catch {
    // Parse error — fail open
    process.exit(0);
  }
});

function logBlock(sessionId, tool, command, reason) {
  try {
    const ts = new Date().toISOString().replace('T', ' ').substring(0, 19) + ' UTC';
    const line = `- [${ts}] BLOCKED session=${sessionId.substring(0, 8)} tool=${tool} reason="${reason}" cmd="${(command || '').substring(0, 80)}"\n`;
    fs.appendFileSync(LOG_FILE, line);
  } catch {
    // Best effort logging
  }
}
