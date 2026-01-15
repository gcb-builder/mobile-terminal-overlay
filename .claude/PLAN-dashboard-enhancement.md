# Mobile Terminal Dashboard Enhancement Plan

**Created:** 2026-01-10T23:45:00Z
**Status:** Pending approval

---

## Overview

Transform the mobile terminal overlay from a "terminal relay" into a full "Claude Code dashboard" with:
- Tabbed interface (Terminal | Context | Touch | Logs | Challenge)
- Offline support with IndexedDB caching and outbox queue
- DeepSeek-Coder-V2 "Challenge" function for skeptical code review
- Persistent status header with connection state

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Status Header (sticky)                   │
│ [WS: ●] [Last: 12:34] [Mode: View] [Auto-scroll: ✓]        │
├─────────────────────────────────────────────────────────────┤
│  Terminal │ Context │ Touch │ Logs │ Challenge             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│                    Active Tab Content                       │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                 [Compose]              [↓ Bottom]           │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Backend API Endpoints

**File:** `mobile_terminal/server.py`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/context` | GET | Return `.claude/CONTEXT.md` from current repo |
| `/api/touch` | GET | Return `.claude/touch-summary.md` |
| `/api/log` | GET | Allowlisted log tail (path + lines params) |
| `/api/challenge` | POST | Build bundle → call DeepSeek → return critique |

### Security

- Log paths allowlist: `runs/latest.log`, `runs/*/logs/*.log`, `embedding_log.txt`
- Redaction patterns: `API_KEY=`, `SECRET=`, `BEGIN PRIVATE KEY`, `Authorization: Bearer`
- Bundle size limit: 20k chars
- Optional `CHALLENGE_TOKEN` env var

---

## Phase 2: Frontend Tabbed UI

**Files:** `index.html`, `styles.css`, `terminal.js`

### Tabs

1. **Terminal** (default) - existing xterm
2. **Context** - renders CONTEXT.md with markdown
3. **Touch** - renders touch-summary.md
4. **Logs** - filterable log viewer with auto-refresh
5. **Challenge** - "Challenge Opus" button + result display

### Dependencies

- `marked.js` for markdown rendering (CDN)

---

## Phase 3: Status Header

Persistent header showing:
- WebSocket connection state (● connected / ○ disconnected)
- Last message timestamp
- Current mode (View/Control/Typing)
- Auto-scroll toggle
- Outbox queue count (when disconnected)

---

## Phase 4: Offline Support

### IndexedDB Schema

```javascript
{
  context: { content, timestamp },
  touch: { content, timestamp },
  logs: { content, timestamp },
  challenge: { content, timestamp },
  outbox: [{ id, type, data, timestamp }]
}
```

### Behavior

- Cache responses on successful fetch
- Show cached data with "[Cached: 5m ago]" indicator when offline
- Queue commands in outbox when disconnected
- Prompt "X items queued - Flush?" on reconnect

---

## Phase 5: Challenge Function (DeepSeek Integration)

### Bundle Content (bounded to 20k chars)

1. Repo name + timestamp
2. `git status -sb` (safe)
3. Current branch name
4. `.claude/CONTEXT.md` (truncated if needed)
5. `.claude/touch-summary.md` (truncated if needed)
6. Last 200 lines of allowlisted log

### System Prompt

```
You are a strict skeptical code reviewer. Do not suggest running commands.
Do not propose code edits. Be concise. Output format:

Risks:
Missing checks/tests:
Clarifying questions (1-3):
```

### vLLM Endpoint

- URL: `http://127.0.0.1:8001/v1/chat/completions`
- Model: whatever vLLM serves
- Temperature: 0.2
- Max tokens: 400

---

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `mobile_terminal/server.py` | Modify | Add 4 new API endpoints |
| `mobile_terminal/challenge.py` | Create | Challenge bundle builder + vLLM client |
| `mobile_terminal/security.py` | Create | Redaction + allowlist logic |
| `mobile_terminal/static/index.html` | Modify | Tabbed UI structure |
| `mobile_terminal/static/styles.css` | Modify | Tab styles, status header |
| `mobile_terminal/static/terminal.js` | Modify | Tab switching, offline support, status |
| `mobile_terminal/static/offline.js` | Create | IndexedDB + outbox queue |
| `mobile_terminal/static/challenge.js` | Create | Challenge tab logic |

---

## Implementation Order

1. Backend API endpoints (without UI)
2. Security layer (redaction + allowlist)
3. Challenge function (vLLM integration)
4. Frontend tabs (basic tab switching)
5. Status header (connection state)
6. Offline support (IndexedDB + outbox)
7. Polish (loading states, error handling)

---

## Estimated Scope

- **New/modified code:** ~800-1000 lines
- **New files:** 4
- **Modified files:** 4

---

## Open Questions

1. Is DeepSeek-Coder-V2 already running on port 8001?
2. Should log paths be discovered from config or hardcoded allowlist?
3. Use CDN for marked.js or keep plain text?
4. Full implementation vs MVP first?

---

## MVP vs Full

### MVP (recommended first)
- Tabs + Context/Touch viewers
- Basic status header
- localStorage caching (simpler than IndexedDB)

### Full
- All features including Challenge
- IndexedDB + outbox queue
- Heartbeat ping/pong
