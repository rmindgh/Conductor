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

// ---------------------------------------------------------------------------
// Shell command safety analysis
// ---------------------------------------------------------------------------
//
// IMPORTANT: This is defense-in-depth against honest mistakes, NOT a security
// boundary against adversarial input. A motivated attacker can always bypass
// these checks via variable expansion, command substitution, encoded payloads,
// or shell wrappers. The primary security boundary is human approval and the
// conductor flag system.
//
// This analyzer tokenizes commands (vs substring/regex match) to catch common
// bypasses like `git push -fu` (where `-f` is inside a combined flag cluster).

/**
 * Naive shell tokenizer — splits on whitespace but respects simple quoting.
 * Not a full shell parser — does not handle $(...), backticks, or escapes beyond basic quotes.
 */
function tokenize(command) {
  const tokens = [];
  let current = '';
  let inSingle = false;
  let inDouble = false;
  let escape = false;

  for (const ch of command) {
    if (escape) {
      current += ch;
      escape = false;
      continue;
    }
    if (ch === '\\' && !inSingle) {
      escape = true;
      continue;
    }
    if (ch === "'" && !inDouble) {
      inSingle = !inSingle;
      continue;
    }
    if (ch === '"' && !inSingle) {
      inDouble = !inDouble;
      continue;
    }
    if (/\s/.test(ch) && !inSingle && !inDouble) {
      if (current) {
        tokens.push(current);
        current = '';
      }
      continue;
    }
    current += ch;
  }
  if (current) tokens.push(current);
  return tokens;
}

function hasForceFlag(tokens) {
  for (const t of tokens) {
    if (!t.startsWith('-')) continue;
    if (t === '--force' || t === '--force-with-lease') return true;
    // Short flag cluster: -f, -fu, -uf, -vfu, etc.
    if (!t.startsWith('--') && t.length > 1 && t.slice(1).includes('f')) {
      return true;
    }
  }
  return false;
}

function hasRecursiveForceFlag(tokens) {
  for (const t of tokens) {
    if (!t.startsWith('-') || t.startsWith('--')) continue;
    const flags = t.slice(1);
    if ((flags.includes('r') || flags.includes('R')) && flags.includes('f')) {
      return true;
    }
  }
  return false;
}

function analyzeBashCommand(command) {
  const tokens = tokenize(command);
  if (tokens.length === 0) return { dangerous: false };

  // Skip leading VAR=value assignments
  let cmdIdx = 0;
  while (cmdIdx < tokens.length) {
    const t = tokens[cmdIdx];
    if (t.includes('=') && !t.startsWith('-') && !t.startsWith('/')) {
      cmdIdx++;
    } else {
      break;
    }
  }
  if (cmdIdx >= tokens.length) return { dangerous: false };

  const cmd = tokens[cmdIdx];
  const args = tokens.slice(cmdIdx + 1);
  const cmdBase = cmd.includes('/') ? cmd.split('/').pop() : cmd;

  // rm with recursive force
  if (cmdBase === 'rm') {
    if (hasRecursiveForceFlag(args)) {
      const targets = args.filter(a => !a.startsWith('-'));
      for (const target of targets) {
        if (['/', '/*', '~', '~/', '*', '.'].includes(target)) {
          return { dangerous: true, reason: `rm -rf against dangerous target: ${target}` };
        }
        if (target.startsWith('/') && (target.match(/\//g) || []).length <= 2) {
          return { dangerous: true, reason: `rm -rf against top-level path: ${target}` };
        }
      }
      return { dangerous: true, reason: 'rm with -rf flag' };
    }
  }

  // git destructive operations
  if (cmdBase === 'git' && args.length > 0) {
    const sub = args[0];
    const subArgs = args.slice(1);

    if (sub === 'push' && hasForceFlag(subArgs)) {
      return { dangerous: true, reason: 'git push with force flag' };
    }
    if (sub === 'reset' && subArgs.includes('--hard')) {
      return { dangerous: true, reason: 'git reset --hard' };
    }
    if (sub === 'clean' && hasForceFlag(subArgs)) {
      return { dangerous: true, reason: 'git clean with force flag' };
    }
    if (sub === 'checkout' && (subArgs.includes('.') || subArgs.includes('--'))) {
      return { dangerous: true, reason: 'git checkout discarding local changes' };
    }
    if (sub === 'branch' && subArgs.includes('-D')) {
      return { dangerous: true, reason: 'git branch -D (force delete)' };
    }
  }

  // NOTE: sh -c / bash -c / python -c / curl | sh are NOT blocked here.
  // They are legitimate commands used every session. The auto-approve layer
  // (remote-control.py) escalates them for human review, but the hook allows
  // them because blocking every exec wrapper would break most sessions.

  // Destructive SQL in SQL contexts
  const sqlContexts = ['psql', 'mysql', 'sqlite3', 'mongo', 'mongosh', 'redis-cli'];
  if (sqlContexts.includes(cmdBase) || tokens.some(t => sqlContexts.some(s => t.includes(s)))) {
    const upper = command.toUpperCase();
    for (const pat of ['DROP TABLE', 'DROP DATABASE', 'TRUNCATE TABLE']) {
      if (upper.includes(pat)) {
        return { dangerous: true, reason: `SQL destructive: ${pat}` };
      }
    }
  }

  // Windows destructive
  if (cmdBase.toLowerCase() === 'format') {
    return { dangerous: true, reason: 'Windows format command' };
  }
  if (cmdBase.toLowerCase() === 'del' && args.some(a => /^\/[sfqSFQ]/.test(a))) {
    return { dangerous: true, reason: 'Windows del /s or /q' };
  }

  return { dangerous: false };
}

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

    // --- Layer 1: Tokenized command analysis (not substring/regex) ---
    if (toolName === 'Bash') {
      const command = toolInput.command || '';
      const analysis = analyzeBashCommand(command);
      if (analysis.dangerous) {
        logBlock(sessionId, toolName, command, analysis.reason);
        process.stdout.write(JSON.stringify({
          decision: 'block',
          reason: `CONDUCTOR: "${command.substring(0, 100)}" blocked — ${analysis.reason}`,
        }));
        return process.exit(0);
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
