# OPENDEV-Inspired Features — Implementation Plan

Based on [arxiv 2603.05344](https://arxiv.org/abs/2603.05344) ("Building AI Coding Agents for the Terminal").

## Wave 1: Tool Output Compression in Log View

**Goal:** Compress verbose tool outputs in the log view with tap-to-expand for mobile UX.

**Immediate mobile UX win, no dependencies.**

- Extend JSONL parsing in `routers/logs.py` to pair `tool_use` with `tool_result` entries and generate summaries (line count, error/success, key patterns)
- Add `structured_messages` to `/api/log` response with typed entries: `{type: "tool", name, summary, has_full, tool_use_id}`
- New endpoint: `GET /api/log/tool-output?tool_use_id={id}` for full output on demand
- Frontend: render tools as compact pills in `renderLogEntriesChunked()` with tap-to-expand
- Summary heuristics:
  - Bash → "Ran `cmd` (42 lines)"
  - Read → "Read file.py (142 lines)"
  - Grep → "23 matches in 5 files"
  - Edit/Write → "Modified file.py (+N/-M lines)"

**Files:** `routers/logs.py`, `terminal.js` (renderLogEntriesChunked)

---

## Wave 2: External Memory / Scratch File Layer

**Goal:** Store full tool outputs externally, serve compressed summaries. Shared scratch space across team agents.

**Core infrastructure enabling deeper compression + team sharing.**

- New `ScratchStore` class: content-addressable storage at `~/.cache/mobile-overlay/scratch/{project_id}/`, SHA256 keyed, LRU eviction at 50MB
- New router `routers/scratch.py`: list, get, get/summary, store, delete endpoints
- Integration with `routers/logs.py`: store full tool outputs, replace with summary + scratch_id in rendered log
- Team sharing: project-scoped store means all agents on the same repo share scratch space
- Future: MCP bridge so agents can call `read_full_output(scratch_id)`

**Storage model:**
- Directory: `~/.cache/mobile-overlay/scratch/{project_id}/`
- Each output: `{content_hash}.json` → `{id, tool_name, target, full_output, summary, timestamp, session_id, pane_id}`
- Index: `_index.jsonl` (append-only manifest)
- Retention: LRU eviction at configurable size (default 50MB)

**Files:** New `routers/scratch.py`, `models.py`, `routers/logs.py`

---

## Wave 3: Context Usage Monitoring

**Goal:** Track and display agent context budget usage. Push alerts at configurable thresholds.

**Extend existing driver + status strip.**

- Add `context_used`, `context_limit`, `context_pct` to `Observation` dataclass in `drivers/base.py`
- Parse token usage from JSONL entries in `ClaudeDriver._classify_entries()`
- Surface in `/api/status/phase` and `/api/team/state` responses
- Status strip: compact pill "ctx 72%" with color coding (green <70%, yellow 70-85%, red >85%)
- Push alert at 85% threshold via `push_monitor()` with 300s cooldown
- New config: `context_alert_threshold: int = 85`

**Files:** `drivers/base.py`, `drivers/claude.py`, `routers/process.py`, `terminal.js`

---

## Wave 4: Planner/Explorer/Executor Role Enforcement

**Goal:** Add typed roles to team agents with tool restrictions enforced at dispatch time.

**Extends team dispatch with typed roles.**

- Role definitions:
  - `explorer` — Read, Glob, Grep, Bash (read-only)
  - `planner` — Read, Glob, Grep, Bash (read-only), EnterPlanMode, TodoWrite
  - `executor` — Full tool access
  - `reviewer` — Read, Glob, Grep, Bash (read-only) + git diff/log
- Role state: `app.state.team_roles: Dict[str, str]` mapping target_id → role
- New endpoints: `POST /api/team/role`, `GET /api/team/roles`
- Dispatch integration: inject role constraints section into dispatch.md (soft enforcement via Claude Code's instruction-following)
- Team view UI: color-coded role badges, role selector dropdown

**Files:** `routers/team.py`, `models.py`, `terminal.js` (team view)

---

## Wave 5: Event Reminders / Stall Detection

**Goal:** Detect stalled agents, inject reminders, push notifications.

**Benefits from all prior features.**

- Stall tracking in `push_monitor()`: per-pane last-progress timestamps, 5min default threshold
- Detection: phase unchanged + log mtime unchanged while agent running = stalled
- Reminder injection: `tmux send-keys` when agent is at prompt (reuse `PROMPT_PATTERNS` detection)
- Escalation: first stall → tmux reminder, second → push notification
- Plan step tracking: parse `.claude/team-memory.md` for incomplete steps
- UI: "stalled" state in status strip (yellow pulse), stall duration in team view
- New config: `stall_timeout_minutes: int = 5`

**Files:** `routers/push.py`, `routers/team.py`, `models.py`

---

## Dependency Graph

```
Wave 1 (Log Compression) ──→ Wave 2 (Scratch Layer) ←── Wave 3 (Context Monitoring)
                                                              │
Wave 4 (Role Enforcement)    (independent)                    │
                                                              ↓
                              Wave 5 (Stall Detection)
```

## Status

| Wave | Feature | Status |
|------|---------|--------|
| 1 | Tool Output Compression | planned |
| 2 | Scratch File Layer | planned |
| 3 | Context Usage Monitoring | planned |
| 4 | Role Enforcement | planned |
| 5 | Stall Detection | planned |
