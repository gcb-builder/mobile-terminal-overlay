# Touch Summary Log

Append-only log of implementation batches.

---

## 2026-03-20: Permission Auto-Approval System

### Files Changed
- `mobile_terminal/models.py` — Added PermissionRequest, PermissionRule, PermissionDecision dataclasses
- `mobile_terminal/server.py` — PermissionPolicy state init, permissions router registration
- `mobile_terminal/routers/terminal_io.py` — Policy-aware permission interception in tail_sender (auto-approve/deny/prompt)
- `mobile_terminal/routers/terminal_sse.py` — Same policy interception for SSE transport
- `mobile_terminal/routers/push.py` — Policy interception in push_monitor (auto-approves when no client connected)
- `mobile_terminal/static/index.html` — Enhanced permission banner (Always·Repo, Always buttons), permissions tab HTML, FAB entry, sidebar section, version bumps (CSS v214, JS v293)
- `mobile_terminal/static/terminal.js` — Always button handlers, extractBaseCommand(), createPermissionRule(), permission_auto toast, permissions tab wiring, activePermissionPayload state
- `mobile_terminal/static/styles.css` — Permission banner button styles (.always-repo, .always), full permissions tab styles (mode toggle, rule items, audit entries)
- `mobile_terminal/static/sw.js` — Cache version bump to v124

### New Files Created
- `mobile_terminal/permission_policy.py` — Risk classifier (HIGH/MEDIUM/LOW patterns), PermissionPolicy class (evaluate, rule matching, defaults, storage, audit), normalize_request()
- `mobile_terminal/routers/permissions.py` — API: GET/POST/DELETE rules, POST mode, GET audit
- `mobile_terminal/static/src/features/permissions.js` — Permissions tab: mode toggle, rule list grouped by scope, delete, audit log

### Risks / Follow-ups
- Audit log grows unbounded (append-only JSONL) — may need rotation for long-running servers
- `extractBaseCommand()` heuristic may not cover all command patterns (currently handles npm/git subcommands)
- Unrestricted mode left as dead code (mode value accepted but not exposed in UI)
- No rule editing in v1 (only create + delete)

---

## 2026-03-19: Backlog Candidate Pipeline (JSONL Interception)

### Files Changed
- `mobile_terminal/models.py` — BacklogCandidate dataclass, CandidateStore class, origin field on BacklogItem
- `mobile_terminal/server.py` — candidate_detector + candidate_store state init
- `mobile_terminal/routers/logs.py` — candidate_detector.set_log_file sync (3 locations)
- `mobile_terminal/routers/terminal_io.py` — Candidate check in tail_sender (every ~2s)
- `mobile_terminal/routers/terminal_sse.py` — Same candidate check for SSE transport
- `mobile_terminal/routers/backlog.py` — Candidate API endpoints (list, keep, dismiss) + origin param on add
- `mobile_terminal/static/src/features/backlog.js` — Candidate tray UI, keep/dismiss handlers, badge counts
- `mobile_terminal/static/terminal.js` — WS routing for backlog_candidate messages
- `mobile_terminal/static/styles.css` — Candidate tray styles (yellow border, slide-in animation)
- `mobile_terminal/static/index.html` — FAB menu backlog entry, version bumps

### New Files Created
- `mobile_terminal/drivers/claude.py` — BacklogCandidateDetector (JSONL incremental reader, TodoWrite/TaskCreate extraction)

### Risks / Follow-ups
- Candidate detection depends on JSONL format stability
- Content hash dedup may miss semantically similar but textually different items

---

## 2026-03-10: Header Reorganization + Pane Quick-Switcher

### Files Changed
- `mobile_terminal/static/index.html` — Header restructured: header-left (connection indicator, context pill, phase indicator) + header-right (sidebar, docs, activity, refresh, push, repo btn/dropdown). Collapse row: replaced duplicate repo switcher with `#recentRepos` pane quick-switcher div. CSS version bump to v172.
- `mobile_terminal/static/styles.css` — `.header-right` gets `position: relative` for dropdown positioning. `.repo-dropdown` opens downward (`top: 100%`, `right: 0`). `.repo-btn` max-width 140px. Added `.recent-repos` and `.recent-repo-btn` styles. Desktop overrides: `.control-bars-container.hidden` forced to `display: flex` so collapse row visible; input/control/role bars force-hidden on desktop; collapse toggle hidden on desktop.
- `mobile_terminal/static/terminal.js` — Added `populateRecentRepos()` (pane-based quick-switcher using `ctx.targets`). Called after `loadTargets()`, `selectTarget()`, and `populateUI()`. Added `extractContextUsage()` for context pill. Removed unused session-tracking code (`recentSessions`, `trackRecentSession`).
- `mobile_terminal/static/dist/terminal.min.js` — Rebuilt bundle.

### New Files Created
- None

### Risks / Follow-ups
- Pane quick-switcher only shows when `ctx.targets.length >= 2` (single-pane sessions show empty collapse row)
- Desktop collapse-row visibility relies on CSS `!important` override of `.control-bars-container.hidden`

---

## 2026-03-04: Batch 4 — Consolidate Duplicate Utility Functions

### Files Changed
- `mobile_terminal/static/terminal.js` — Removed 2 duplicate `escapeHtml()` definitions (DOM-based closure version at former line 7282 and module-scope version at former line 11109); kept string-replacement version with added null guard. Removed `formatBytes()` (closure, former line 7522) and replaced call with `formatFileSize()`. Removed `formatRelativeTime()` (closure, former line 7509) and replaced call with `formatTimeAgo(s.modified * 1000)`. Net: 11887 -> 11846 lines (-41 lines).
- `mobile_terminal/server.py` — Added `get_project_id(repo_path, strip_leading=False)` helper near top of file; replaced 6 inline `str(repo_path.resolve()).replace(...)` calls and 1 `.lstrip("-")` variant. Added `_read_claude_file(filename, label)` helper; refactored `/api/context`, `/api/touch`, `/api/docs/context`, `/api/docs/touch` to use it (4 endpoints -> 4 thin wrappers + 1 helper). Net: 7879 -> 7813 lines (-66 lines).

### New Files Created
- None

### Risks / Follow-ups
- `formatTimeAgo()` returns compact format ("5m") vs old `formatRelativeTime()` which returned "5m ago". The shorter format is consistent with how the rest of the app displays time in compact contexts.
- The `get_project_id` helper with `strip_leading=True` preserves the one call site that used `.lstrip("-")`. Both path formats continue to work as before.

---

## 2026-03-04: Batch 2C — Server + HTML Dead Code Cleanup

### Files Changed
- `mobile_terminal/server.py` — Removed 3 unused top-level imports (`atexit`, `deque`, `find_claude_log_file`); removed duplicate `/restart` endpoint; removed dead PermissionDetector comment; removed 33 redundant local imports (`import subprocess`, `import json`, `import time`, `import re`, `import os`, `import signal`, `import time as _time`, `import json as json_mod`); replaced all `_time.time()` with `time.time()` and `json_mod.loads()` with `json.loads()`. Net: 7932 -> 7879 lines (-53 lines).
- `mobile_terminal/static/index.html` — Removed 4 dead element IDs (`teamViewHeader`, `terminalPanelHeader`, `devPreviewContainer`, `mcpRestartText`); removed 2 inline `onclick` attributes from reconnectBtn/hardRefreshBtn; normalized 4 self-closing `<input ... />` to `<input ...>`.
- `mobile_terminal/static/terminal.js` — Added `addEventListener('click', ...)` for `reconnectBtn` (-> `manualReconnect`) and `hardRefreshBtn` (-> `hardRefresh`) in DOMContentLoaded block.

### New Files Created
- None

### Risks / Follow-ups
- None — all changes are mechanical dead code removal verified by syntax check

---

## 2026-03-03: Queue Insert-to-Edit + Per-Pane Scoping

### Files Changed
- `mobile_terminal/server.py` — Added `_queue_key()` static method to CommandQueue, added `pane_id` param to all CommandQueue methods (`_get_queue`, `_load_from_disk`, `_save_to_disk`, `enqueue`, `dequeue`, `reorder`, `list_items`, `pause`, `resume`, `is_paused`, `flush`, `get_next_unsafe`, `_send_item`, `send_next_unsafe`), added `pane_id` query param to all queue API endpoints, updated `_process_loop` to use `active_target`, updated `get_queue_file`/`load_queue_from_disk`/`save_queue_to_disk` for pane_id
- `mobile_terminal/static/terminal.js` — Replaced `sendNextUnsafe()` with `insertNextToInput()`, added `reorderQueueItem()`, updated `renderQueueList()` with reorder buttons and tap-to-insert handlers, updated `getQueueStorageKey()` to include pane, added `pane_id` to all queue API calls, added queue save/load/reconcile in `selectTarget()`, updated `setupQueue()` wiring
- `mobile_terminal/static/styles.css` — Added `.queue-item[data-status="queued"]` cursor/active styles, added `.queue-item-reorder` and `.queue-reorder-btn` styles
- `mobile_terminal/static/index.html` — Renamed "Send Next" to "Insert", bumped versions (styles v169, terminal.js v274)

### Risks / Follow-ups
- Old localStorage keys (`mto_queue_<session>`) won't match new keys (`mto_queue_<session>:<pane>`); existing queued items in localStorage will be orphaned on first load after upgrade (acceptable — reconcileQueue will re-sync from server)
- Queue reorder error handling: on server error, items are swapped back locally but server state may diverge until next reconcile

---

## 2026-03-02: Prompt Banner "Other" Option with Textarea Input

### Files Changed
- `mobile_terminal/static/terminal.js` — Modified `showPromptBanner()` (detect "Other" choices, add `many-choices` class for 4+ options, mark buttons with `data-other`), modified `setupPromptBannerHandlers()` (route "Other" buttons to `showOtherInput()`), added 3 new functions: `showOtherInput()`, `restorePromptChoices()`, `sendOtherFeedback()`
- `mobile_terminal/static/styles.css` — Added `.many-choices` vertical stack layout for 4+ choices, `.prompt-other-input` textarea area with Back/Send buttons, bumped `.prompt-banner` max-height from 50vh to 60vh

### New Files Created
- None

### Risks / Follow-ups
- The 50ms delay between choice send and Ctrl+U + feedback send is a heuristic; may need tuning if Claude Code's line editor takes longer to activate
- Mobile keyboard auto-focus (`setTimeout(() => textarea.focus(), 50)`) may not work on all mobile browsers due to user-gesture requirements

---

## 2026-03-02: Desktop Responsive Multi-Pane Layout

**Goal:** Add responsive desktop layout (>=1024px) that shows Team + Log simultaneously as a multi-pane grid, with Terminal as a dockable bottom panel. Mobile behavior unchanged.

### Files Changed
- `mobile_terminal/static/index.html` — Wrapped views in `#viewsContainer`, restructured `#teamView` with stable header (`#teamViewHeader`) + cards container (`#teamCardsContainer`), added density toggle, filter bar, terminal panel header with close button. Version bumps: styles.css?v=168, terminal.js?v=273
- `mobile_terminal/static/styles.css` — Added `--sidebar-width`, `--terminal-panel-height` CSS vars. Added `.views-container` base styles. Added `@media (min-width: 1024px)` block with: CSS grid shell (sidebar + main), team sidebar always-visible, log main area, terminal bottom dock panel, density variants (comfortable/compact/ultra), team filter bar, search input, filter chips, agent selection highlight, hover actions, pane focus indicator, shortcut help modal, terminal panel header/resize styles. ~546 new lines.
- `mobile_terminal/static/terminal.js` — Added `uiMode`, `desktopFocusedPane`, singleflight guards (`logRefreshInFlight`, `teamRefreshInFlight`), `shouldLogRefreshRun()`/`shouldTeamRefreshRun()` guard helpers. Modified: `switchToView()` (desktop routing), `hideAllContainers()` (desktop early-return), `switchToTerminalView()` (desktop redirect), `setupSwipeNavigation()` (desktop skip), `startTeamCardRefresh()` (use guard helper), `refreshTeamCards()` (singleflight + guard), `startLogAutoRefresh()` (use guard helper), `renderTeamCards()` (write to `#teamCardsContainer`, track agent names, restore selection, apply filters). New functions: `setupDesktopLayout()`, `checkDesktopLayout()`, `enterDesktopLayout()`, `exitDesktopLayout()`, `switchDesktopFocus()`, `openDesktopTerminal()`, `closeDesktopTerminal()`, `setupDesktopTerminalResize()`, `setupDensityToggle()`, `applyDensity()`, `setupTeamFilters()`, `applyTeamFilters()`, `setupDesktopShortcuts()`, agent selection helpers (j/k/a/d/Enter), `toggleShortcutHelp()`, `addDesktopHoverActions()`. ~686 new lines.

### New Files
None

### Risks / Follow-ups
- Right-dock terminal layout deferred (bottom-only for now)
- xterm write queue safety valve (500KB backlog drop) not implemented — deferred
- Shortcut help modal is JS-generated (not in HTML) — simpler but less discoverable
- Team filter "working" maps to "active" section, which includes planning/waiting states — may need refinement

---

## 2026-02-25: Mobile Layout Hierarchy — Urgency-Driven Team UI

**Goal:** Reimpliment mobile UI with information hierarchy where attention flows based on urgency.

### Files Changed
- `mobile_terminal/static/index.html` — DOM restructure (status strip → view switcher → banners → views → action bar), system status strip, connection banner, log filter bar, terminal agent selector
- `mobile_terminal/static/styles.css` — View switcher, team sections, card redesign with badges, urgency visual weights, permission action buttons, system status strip (48px), action bar, log filter bar, terminal agent selector, connection state banners, reduced-motion support
- `mobile_terminal/static/terminal.js` — UIState mapping layer (deriveUIState, deriveSystemSummary), view switcher logic, section-based team rendering, badge-driven cards, contextual action bar, system status updates, log event classifier + filtering, terminal agent selector, connection state banners

### New Files
None (all edits to existing files)

### Risks / Follow-ups
- Legacy viewBar hidden but still in DOM for JS references — can be fully removed when action bar delegates are verified
- Log filter "agent" dropdown not yet wired to actual agent filtering (type filtering works)
- Dispatch bottom sheet (Phase 3c spec) deferred — uses existing dispatch bar for now
- Log view restructuring is client-side filtering only — no server-side event classification yet

---

## 2026-02-25: Agent Driver Layer — Make MTO Agent-Agnostic

**Goal:** Introduce pluggable AgentDriver layer separating terminal orchestration from agent semantics. Support Claude, Codex, Gemini CLI, or any agent with graceful degradation.

**Files changed:**
- `mobile_terminal/config.py` — Added `agent_type`, `agent_display_name` fields + serialization
- `mobile_terminal/cli.py` — Added `--agent-type` CLI argument
- `mobile_terminal/drivers/__init__.py` — NEW: Driver registry with `get_driver()`, `register_driver()`
- `mobile_terminal/drivers/base.py` — NEW: AgentDriver protocol, Observation, ObserveContext, BaseAgentDriver, JSONL utils
- `mobile_terminal/drivers/claude.py` — NEW: ClaudeDriver (extracted from server.py) + ClaudePermissionDetector
- `mobile_terminal/drivers/codex.py` — NEW: CodexDriver (proof of pattern)
- `mobile_terminal/drivers/generic.py` — NEW: GenericDriver (stdout-regex heuristics)
- `mobile_terminal/server.py` — Deleted PermissionDetector, _detect_phase, _detect_phase_for_cwd (~600 LOC). Added driver init, _build_observe_context, /api/health/agent + /api/agent/start endpoints (aliases kept). Updated push_monitor + WS permission check + team state endpoints.
- `mobile_terminal/static/index.html` — Renamed claude→agent IDs, dynamic crash banner text
- `mobile_terminal/static/terminal.js` — ~30 variable/function/API-URL renames, dynamic agentName from /config
- `mobile_terminal/static/styles.css` — Renamed claude→agent CSS classes
- `mobile_terminal/static/sw.js` — Updated push notification event type
- `tests/test_drivers.py` — NEW: 37 tests for JSONL parsing, phase classification, permission detection, registry, PID detection

**New files:** 6 (drivers/*, tests/*)
**Risks:** Response shape change on /api/health/claude (now returns Observation JSON, not old claude_running/claude_pid shape). Frontend updated simultaneously.

---

## 2026-02-24: Team View - Consolidated Agent Cards

**Goal:** Add a card-based Team View as a swipeable tab between Log and Terminal, showing agent status, branch, tail text, and Allow/Deny action buttons.

**Files Changed:**
- `mobile_terminal/server.py` - Added `GET /api/team/capture` (batch pane capture with cache), `POST /api/team/send` (targeted input to team pane)
- `mobile_terminal/static/index.html` - Added `#teamView` div, removed static tab dots (now dynamic), version bumps (styles.css v154, terminal.js v254)
- `mobile_terminal/static/styles.css` - Added `.team-view`, `.team-cards-grid` (responsive 2-column), `.team-card` header/body/footer/button styles
- `mobile_terminal/static/terminal.js` - Replaced `tabOrder` const with `getTabOrder()`, rewrote `updateTabIndicator()` for dynamic dots, added `switchToTeamView()`, card rendering (`refreshTeamCards`, `createTeamCard`, `sendTeamInput`), auto-refresh (5s interval), team presence transitions in `updateTeamState()`

**New Files:** None

**Risks / Follow-ups:**
- Phase 2 (deferred): Tabbed per-agent JSONL event timeline from cards view
- Capture endpoint runs sequentially (~20ms for 3-4 panes); could parallelize if team sizes grow

---

## 2026-01-10: Initial Scaffold

**Goal:** Create initial project structure for mobile-terminal-overlay

**Files Created:**
- `pyproject.toml` - pip package configuration
- `mobile_terminal/__init__.py` - package init
- `mobile_terminal/config.py` - Config dataclass with YAML loading
- `mobile_terminal/discovery.py` - Auto-discovery from CLAUDE.md
- `mobile_terminal/server.py` - FastAPI + WebSocket tmux relay
- `mobile_terminal/cli.py` - CLI entrypoint
- `mobile_terminal/static/index.html` - Main UI with xterm.js
- `mobile_terminal/static/styles.css` - Mobile-first CSS
- `mobile_terminal/static/terminal.js` - WebSocket + xterm.js client
- `README.md` - Documentation
- `.gitignore` - Git exclusions
- `CLAUDE.md` - Claude Code guidelines
- `.claude/CONTEXT.md` - Session context
- `.claude/touch-summary.md` - This file

**Risks/Follow-ups:**
- Needs testing with actual tmux session
- Needs unit tests
- Role prefix regex may need tuning for different CLAUDE.md formats

---

## 2026-01-15: Copy/Select Fix + PWA Standalone

**Goal:** Fix copy functionality breaking terminal input, add PWA standalone support

**Files Changed:**
- `mobile_terminal/server.py` - Robust message handling, continue on errors instead of disconnecting
- `mobile_terminal/static/terminal.js` - Focus restoration after copy, isComposing reset, rewritten copy handler
- `mobile_terminal/static/index.html` - Added ^B button, service worker registration
- `mobile_terminal/static/manifest.json` - Added scope and display_override for standalone
- `mobile_terminal/static/styles.css` - Various UX improvements

**Files Created:**
- `mobile_terminal/static/sw.js` - Service worker for PWA installation
- `mobile-terminal.service` - systemd unit file for auto-start

**Commits:**
- `da6e940` Fix copy/select bug and add PWA standalone support
- `3d25636` Add Ctrl+B (tmux prefix) to control bar

**Risks/Follow-ups:**
- PWA needs reinstall after service worker changes
- systemd service needs manual setup (sudo required)

---

## 2026-01-18: Input Box Sync with tmux Terminal

**Goal:** Implement tighter alignment between mobile overlay input box and tmux terminal command line

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Added extractPromptContent(), syncPromptToInput(), sendKeyWithSync(), sendKeyDebounced(); atomic send in sendLogCommand(); modified control bar handlers for Up/Down/Tab sync
- `mobile_terminal/static/index.html` - Added sync button (↻) to terminal-block-input, version bumps
- `mobile_terminal/static/styles.css` - Added .terminal-sync-btn styling
- `mobile_terminal/static/sw.js` - Version bump to v31

**New Functions (terminal.js):**
- `extractPromptContent(content)` - Multi-pattern prompt detection (Claude ❯, bash $, Python >>>, Node >)
- `syncPromptToInput()` - Captures terminal and syncs prompt content to input box
- `sendKeyWithSync(key, delay)` - Sends key and syncs result back to input
- `sendKeyDebounced(key, force)` - 150ms debounce, Ctrl+C bypasses

**Commits:**
- `9bd1ea9` Add input box sync with tmux terminal command line

**Risks/Follow-ups:**
- Sync delay (100-200ms) may feel slow on laggy connections
- Prompt patterns may need expansion for other shells (fish, nushell)

---

## 2026-01-18: UI Simplification (continued)

**Goal:** Remove lock button, simplify controls, fix tail viewport filtering

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Removed toggleControl(), controlBtn; set isControlUnlocked=true; empty Enter sends '\r'; fixed ⏵⏵ filter to not hide Claude options
- `mobile_terminal/static/index.html` - Removed lock button, moved collapse toggle to tab indicator
- `mobile_terminal/static/styles.css` - Resized collapse toggle (32x24px pill) for tab bar

**Commits:**
- `eb4e898` Fix tail viewport filtering and empty Enter behavior
- `298ea42` Remove lock button, move collapse toggle to tab indicator

**Changes:**
- Controls always enabled (no lock/unlock)
- Collapse toggle in tab indicator section (next to dots)
- Empty input Enter confirms prompts (like control bar Enter)
- ⏵⏵ filter now only hides status hints, not interactive options

---

## 2026-01-18: Header Simplification and Unified Collapse

**Goal:** Streamline header, remove redundant indicators, unify collapse behavior

**Files Changed:**
- `mobile_terminal/static/index.html` - Removed "Log" title and log-view-header; removed sync button from input box; added refresh button to header; removed working indicator
- `mobile_terminal/static/styles.css` - Removed .log-view-header, .terminal-sync-btn, .header-thinking styles; added .view-bar.collapsed
- `mobile_terminal/static/terminal.js` - Removed updateWorkingIndicator(), showThinking(), hideThinking(), maybeHideThinking(); updated toggleControlBarsCollapse() to also collapse viewBar; refresh button now calls loadLogContent() + syncPromptToInput()
- `mobile_terminal/static/sw.js` - Version bump to v32

**Commits:**
- `45ddc35` Simplify header and unify collapse behavior

**Changes:**
- Removed "Log" title from log view
- Refresh button moved to header (refreshes log + syncs input)
- Removed sync button from input box
- Removed "working..." indicator (redundant with activity timestamp)
- View bar (Select/Stop/Challenge/Compose) collapses with control bars
- Control bar Enter kept as stateless confirm (ignores input box state)

---

## 2026-01-18: Non-Blocking Tool Collapse for Log View

**Goal:** Collapse consecutive duplicate tool `<details>` blocks into single block with "×N" badge

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Added data-tool attributes in renderLogEntries(), scheduleCollapse(), collapseRepeatedTools(), setupCollapseHandler()
- `mobile_terminal/static/styles.css` - Added .collapsed-duplicate, .collapse-count styles
- `mobile_terminal/static/index.html` - Version bumps (v120, v151, v34)
- `mobile_terminal/static/sw.js` - Version bump to v34

**New Functions (terminal.js):**
- `scheduleCollapse()` - Hash-based scheduling via requestIdleCallback
- `collapseRepeatedTools(hash)` - Single-pass DOM collapse with badge insertion
- `setupCollapseHandler()` - Event delegation for expand/collapse toggle

**Key Design Decisions:**
- Post-render collapse via requestIdleCallback (fallback: setTimeout 100ms)
- Hash check: `${toolCount}:${lastToolKey}:${htmlLength}` to skip unchanged content
- Stable group keys: `${toolName}:${summaryText.slice(0,40)}` (not index-based)
- Single event delegation handler (no per-node listeners)
- Graceful degradation: try/catch wrapper, base render unaffected

**Risks/Follow-ups:**
- Previous attempts caused lag due to work in render loop; this uses idle callback
- Safari <16.4 uses setTimeout fallback
- Hash verification at end of collapse to detect race conditions

---

## 2026-01-19: Smart Auto-Scroll and Plan File Previews

**Goal:** Improve log view UX with conditional auto-scroll and inline plan file previews

**Problem 2: Auto-scroll Interrupts Reading**
- Track if user is at bottom of log via scroll event listener
- Only auto-scroll if `userAtBottom` is true
- Show floating "↓ New content" indicator when new content arrives while reading
- Click indicator to jump to bottom

**Problem 3: Plan Files Not Visible**
- Added `/api/plan` endpoint to read plan files from `~/.claude/plans/`
- Detect plan file paths in log text via regex
- Replace with clickable elements showing filename
- On tap, fetch 10-line preview and show inline
- Tap again to collapse

**Files Changed:**
- `mobile_terminal/server.py` - Added `/api/plan` endpoint with filename sanitization
- `mobile_terminal/static/terminal.js` - Added scroll tracking, new content indicator, plan file detection
- `mobile_terminal/static/styles.css` - Added .new-content-indicator, .plan-file-ref, .plan-preview styles
- `mobile_terminal/static/index.html` - Version bumps (v121, v152, v35)
- `mobile_terminal/static/sw.js` - Version bump to v35

**New Functions (terminal.js):**
- `setupScrollTracking()` - Scroll event listener to track userAtBottom
- `showNewContentIndicator()` / `hideNewContentIndicator()` - Floating indicator management
- `schedulePlanPreviews()` - Schedule plan detection for idle time
- `detectAndReplacePlanRefs()` - TreeWalker to find and replace plan paths
- `setupPlanPreviewHandler()` - Event delegation for plan preview clicks

**Key Design Decisions:**
- Both features use requestIdleCallback for non-blocking execution
- New content indicator positioned absolute, slides up with animation
- Plan file regex: `/(?:~|\/home\/\w+)\/\.claude\/plans\/([\w\-\.]+\.md)/g`
- API sanitizes filenames to prevent path traversal

---

## 2026-01-19: Super-Collapse for Tool Runs

**Goal:** Collapse runs of many consecutive tool calls into single summary row

**Problem:** Even with same-tool collapsing (×N badges), interleaved tool types (Read, Edit, TodoWrite, Grep) still create long lists that overwhelm the log view.

**Solution:** "Super-collapse" - when 6+ consecutive tool blocks appear, collapse them into:
```
🔧 23 tool operations ▶
```
Click to expand and see individual tools (which can still have ×N badges).

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Added scheduleSuperCollapse(), applySuperCollapse(), createSuperGroup(), setupSuperCollapseHandler()
- `mobile_terminal/static/styles.css` - Added .super-collapsed, .tool-supergroup, .tool-supergroup-toggle styles
- `mobile_terminal/static/index.html` - Version bumps (v122, v153, v36)
- `mobile_terminal/static/sw.js` - Version bump to v36

**New State Variables:**
- `SUPER_COLLAPSE_THRESHOLD = 6` - Minimum tools to trigger super-collapse
- `lastSuperCollapseHash` - Skip work if unchanged
- `expandedSuperGroups` - Set of expanded group keys

**New Functions (terminal.js):**
- `scheduleSuperCollapse()` - Waits 150ms for regular collapse, then schedules idle pass
- `applySuperCollapse(hash)` - Finds runs of consecutive .log-tool within each .log-card-body
- `createSuperGroup(container, tools, insertIndex)` - Creates header element, hides tools
- `setupSuperCollapseHandler()` - Event delegation for toggle clicks

**Key Design Decisions:**
- Runs after 150ms delay to let regular collapse (×N badges) complete first
- Uses requestIdleCallback with 700ms timeout for non-blocking execution
- Stable group key: `supergroup:${firstToolKey}:${toolCount}`
- Super-groups are per-.log-card-body (within a Claude turn), not global
- CSS uses `!important` on .super-collapsed to ensure tools are hidden

---

## 2026-01-19: Terminal-Native UI Design (CSS Only)

**Goal:** Make UI feel like one coherent terminal with layers, not three separate widgets

**Problem:** UI felt like chat app (YOU/CLAUDE badges, rounded cards) mixed with terminal, causing mental model switching.

**Solution:** Terminal-first aesthetic throughout:

**Log cards → Terminal style:**
- Removed rounded corners, chat-style backgrounds
- Hidden YOU/CLAUDE role badges
- Monospace font for all text
- User messages prefixed with `$ ` in accent color
- Left border indicator only (2px, not full card)

**Log → Terminal handoff:**
- Added gradient fade (::before pseudo-element)
- "── live ──" label at top of terminal block
- Visual continuity from history to active

**Input → stdin feel:**
- Darker background matching terminal aesthetic
- Subtle caret color (accent)
- Placeholder opacity reduced

**Control strip → Keyboard extension:**
- Darker background (--bg-darker)
- Smaller, tighter buttons (36px)
- Monospace font on quick-btn
- Reduced gap and padding

**Files Changed:**
- `mobile_terminal/static/styles.css` - Major CSS refactor (~100 lines changed)
- `mobile_terminal/static/index.html` - Version bump (v123)
- `mobile_terminal/static/sw.js` - Version bump (v37)

**Key CSS Changes:**
- `.log-card` - border-radius: 0, no background
- `.log-card-header` - display: none (hides role badges)
- `.log-text` - monospace font
- `.log-text.user-text::before` - content: '$ '
- `.terminal-block::before` - gradient fade overlay
- `.terminal-block::after` - "live" label
- `.quick-btn` - monospace, smaller, darker

---

## 2026-01-19: Collapse Toggle Fix

**Goal:** Fix collapse button not properly expanding control bars

**Problem:** When the collapse button was tapped to collapse the control bars, tapping again to expand might not work if the `hidden` class was present (e.g., from a view switch while collapsed).

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Fixed toggleControlBarsCollapse() to also remove 'hidden' class when expanding in log/terminal views
- `mobile_terminal/static/index.html` - Version bump (v154, v38)
- `mobile_terminal/static/sw.js` - Version bump to v38

**Fix Details:**
- Modified `toggleControlBarsCollapse()` to remove `hidden` class when expanding (isCollapsed=false)
- Only applies in log and terminal views where bars should be visible
- Ensures bars become visible even if `hidden` was re-added during view switches

**Commits:**
- `46e64fb` Fix collapse toggle to properly expand control bars

---

## 2026-01-19: Connection Stability Improvements

**Goal:** Reduce "Connecting..." screen frequency on mobile networks

**Problems Addressed:**
1. Heartbeat too slow (30s ping, 10s timeout = 40s to detect dead connection)
2. No server-side ping (server only responds, never initiates)
3. No idle connection detection (stale connections not detected until heartbeat)
4. Aggressive reconnect backoff (up to 30s delay)

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Faster heartbeat, idle check, server ping handling
- `mobile_terminal/server.py` - Server-side keepalive pings, pong handling
- `mobile_terminal/static/index.html` - Version bump (v155)

**Changes:**
1. **Faster heartbeat**: 15s interval (was 30s), 5s timeout (was 10s)
2. **Faster reconnect**: 300ms initial (was 500ms), 10s max (was 30s)
3. **Idle detection**: Check every 5s, ping if no data for 20s
4. **Server-side ping**: Server sends ping every 20s to keep connection alive
5. **Bidirectional keepalive**: Both client and server can initiate pings

**New Functions (terminal.js):**
- `startIdleCheck()` - Monitors connection idle time, sends keepalive pings

**New Variables:**
- `lastDataReceived` - Timestamp of last data from server
- `idleCheckTimer` - Timer for idle connection checks
- `IDLE_THRESHOLD` - 20s threshold for idle detection

**Commits:**
- (pending)

---

## 2026-01-19: Preview v0 - UI State Time-Travel

**Goal:** Add a "Preview" system for safe read-only time-travel through UI state snapshots.

**Files Changed:**
- `mobile_terminal/server.py` - Added SnapshotBuffer class, 4 preview API endpoints
- `mobile_terminal/static/terminal.js` - Preview mode state, capture/enter/exit functions, input guards
- `mobile_terminal/static/index.html` - Preview banner, drawer HTML, Preview button in view bar
- `mobile_terminal/static/styles.css` - Preview banner and drawer styles
- `mobile_terminal/static/sw.js` - Version bump to v39

**New Server Components:**
- `SnapshotBuffer` class - Ring buffer (200 max) with MD5 hash deduplication
- `POST /api/rollback/preview/capture` - Capture snapshot (JSONL + tmux + queue)
- `GET /api/rollback/previews` - List snapshots (id, timestamp, label)
- `GET /api/rollback/preview/{snap_id}` - Get full snapshot data
- `POST /api/rollback/preview/select` - Enter/exit preview mode

**New Client Functions (terminal.js):**
- `captureSnapshot(label)` - Trigger server-side capture
- `loadSnapshotList()` - Fetch available snapshots
- `enterPreviewMode(snapId)` - Load snapshot and disable inputs
- `exitPreviewMode()` - Return to live, re-enable inputs
- `renderPreviewLog()` / `renderPreviewTerminal()` - Display snapshot data
- `showPreviewBanner()` / `hidePreviewBanner()` - Preview mode indicator
- `disableInputsForPreview()` / `enableInputsAfterPreview()` - Input control
- `isPreviewMode()` - Check if in preview mode
- `setupPreviewHandlers()` - Wire up UI event handlers

**Snapshot Schema:**
```
{id, timestamp, session, label, log_entries, log_hash, terminal_text, queue_state}
```

**Labels:** user_send, periodic (30s), manual (+ Snap button)

**UI Components:**
- Preview banner (fixed, yellow, "Preview Mode" + timestamp + "Back to Live")
- Preview drawer (bottom sheet, snapshot list, + Snap button)
- Preview button in view bar

**Safety:**
- All inputs disabled in preview mode (log input, quick buttons, compose)
- terminal.onData() blocked
- sendLogCommand() blocked
- Purely read-only display

**Commits:**
- (pending)

---

## 2026-01-19: Rollback v1 - Preview Enhancements + Git Rollback

**Goal:** Enhance Preview v0 with pin/export/diff/audit features and add Git rollback capabilities.

### Stage A: Preview v1 (Read-Only Enhancements)

**Server Changes (server.py):**
- Added `AuditLog` class - In-memory audit log with 500 entry limit
- Added `pin_snapshot()` method to SnapshotBuffer - Prevents pinned snapshots from eviction
- Modified eviction logic to skip pinned snapshots
- Updated `list_snapshots()` to include `pinned` field
- Added audit logging to capture, select, pin, export endpoints

**New Endpoints:**
- `POST /api/rollback/preview/{snap_id}/pin` - Pin/unpin snapshot
- `GET /api/rollback/preview/{snap_id}/export` - Download snapshot as JSON
- `GET /api/rollback/preview/diff` - Compare two snapshots
- `GET /api/rollback/audit` - Get audit log entries

**Client Changes (terminal.js):**
- Updated `renderPreviewList()` to show pin/export buttons
- Added `toggleSnapshotPin(snapId, pinned)` - Toggle pin via API
- Added `exportSnapshot(snapId)` - Trigger JSON download
- Added click handlers for pin/export in event delegation

**Styles (styles.css):**
- `.preview-pin-btn` - Pin toggle button (pushpin icons)
- `.preview-export-btn` - Export download button
- `.preview-list-item.pinned` - Yellow border for pinned snapshots

### Stage B: Git Rollback v1 (Non-Destructive)

**New Git Endpoints (server.py):**
- `GET /api/rollback/git/commits` - List recent commits (hash, subject, author, date)
- `GET /api/rollback/git/commit/{hash}` - Get commit detail (body, stat)
- `POST /api/rollback/git/revert/dry-run` - Preview revert without executing
- `POST /api/rollback/git/revert/execute` - Execute git revert, return undo_target
- `POST /api/rollback/git/revert/undo` - Reset to pre-revert state

**Safety Measures:**
- Hash validation with regex (`^[a-f0-9]{7,40}$`)
- Clean working directory check before any write operation
- Subprocess timeouts (5-30s depending on operation)
- Audit logging for all operations
- No force operations

**UI Changes (index.html):**
- Converted preview drawer to tabbed interface (Preview / Git tabs)
- Added git tab content: commit list, detail panel, action buttons
- Detail panel shows: subject, body, author, date, file stat
- Action buttons: Dry Run, Revert, Undo

**Client Functions (terminal.js):**
- `switchRollbackTab(tabName)` - Switch between Preview/Git tabs
- `loadGitCommits()` - Fetch and render commits list
- `renderGitCommitList()` - Display commits in list
- `showGitCommitDetail(hash)` - Load and display commit detail
- `showGitCommitList()` - Return to list view
- `dryRunRevert()` - Preview revert changes
- `executeRevert()` - Execute revert with confirmation
- `undoRevert()` - Undo last revert
- `escapeHtml(text)` - XSS prevention

**Styles (styles.css):**
- `.rollback-tabs` / `.rollback-tab` - Tab buttons
- `.rollback-tab-content` - Tab panels
- `.git-commit-list` / `.git-commit-item` - Commit list
- `.git-commit-detail` - Detail panel
- `.git-action-btn` - Action buttons (secondary, danger)
- `.git-dry-run-result` - Result display (success/error states)

**Files Changed:**
- `mobile_terminal/server.py` - AuditLog, pin logic, 9 new endpoints
- `mobile_terminal/static/terminal.js` - Pin/export, git tab functions
- `mobile_terminal/static/index.html` - Tabbed drawer, git tab HTML
- `mobile_terminal/static/styles.css` - Tab and git styles
- `mobile_terminal/static/sw.js` - Version bump to v41

**Version Bumps:**
- styles.css: v124 → v126
- terminal.js: v158 → v160
- sw.js: v39 → v41

**Commits:**
- (pending)

---

## 2026-01-21: Target Safety Checks, Log Scroll Fix, Drawer Backdrop

**Goal:** Add safety features for multi-project workflows, fix log scroll jumping, improve drawer UX

### Target Safety Checks

**Problem:** Actions could execute against wrong project if target changed mid-request.

**Solution:** Server validates session+pane_id on all action endpoints.

**Server Changes (server.py):**
- Added `validate_target(session, pane_id)` helper function
- Returns 409 Conflict with expected vs received values on mismatch
- Updated 5 action endpoints: `/api/rollback/git/revert/execute`, `/api/process/terminate`, `/api/process/respawn`, `/api/runner/execute`, `/api/runner/custom`

**Client Changes (terminal.js):**
- Added `getTargetParams()` helper - returns `session=X&pane_id=Y`
- All 5 action API calls now include target params

### Log Scroll Fix

**Problem:** When new content arrived while user was scrolling/reading, the log would jump to random positions because innerHTML replacement resets scroll.

**Solution:** Don't re-render while user is scrolling - defer until they scroll to bottom.

**Changes (terminal.js):**
- Added `pendingLogContent` variable to store content during scroll
- `refreshLogContent()` - If `!userAtBottom`, store content and show indicator, skip render
- `setupScrollTracking()` - Render pending content when user scrolls to bottom
- Indicator click handler - Render pending content before scrolling
- `renderLogEntries()` - Simplified, always scrolls to bottom (only called when appropriate)

### Drawer Backdrop

**Problem:** No way to close drawer by tapping outside.

**Solution:** Added semi-transparent backdrop overlay.

**Changes:**
- `index.html` - Added `<div id="drawerBackdrop">` before drawer
- `styles.css` - Added `.drawer-backdrop` styles (fixed overlay, z-index 340, fadeIn animation)
- `terminal.js` - `openDrawer()` and `openDrawerWithQueueTab()` show backdrop; `closePreviewDrawer()` hides it; backdrop click closes drawer

### Version Bump

- `pyproject.toml` - Version 0.1.0 → 0.2.0

**Files Changed:**
- `mobile_terminal/server.py` - validate_target(), target validation on 5 endpoints
- `mobile_terminal/static/terminal.js` - getTargetParams(), pendingLogContent, backdrop handling
- `mobile_terminal/static/index.html` - drawerBackdrop element
- `mobile_terminal/static/styles.css` - .drawer-backdrop styles
- `pyproject.toml` - version bump

**Commits:**
- `578fc03` Add target safety checks, fix log scroll, add drawer backdrop

---

## 2026-01-23: Dev Preview Tab & UX Polish

**Goal:** Add Replit-like dev preview tab, improve Challenge with plan selector, polish UI

### Dev Preview Tab (Replit-like)

**Files Changed:**
- `mobile_terminal/server.py` - Added 4 preview endpoints: `/api/preview/config`, `/api/preview/status`, `/api/preview/start`, `/api/preview/stop`
- `mobile_terminal/static/index.html` - Added Dev tab button and content in drawer
- `mobile_terminal/static/terminal.js` - Added preview module (~200 lines): loadPreviewConfig, renderPreviewServices, selectPreviewService, startPreviewService, stopPreviewService, refreshPreviewStatus, buildPreviewUrl
- `mobile_terminal/static/styles.css` - Added dev preview styles (.dev-status-banner, .dev-service-tabs, .dev-controls, .dev-preview-frame)

**Config Format:** `preview.config.json` per repo with services array and tailscaleServe settings

### Challenge Plan Selector

**Files Changed:**
- `mobile_terminal/server.py` - Changed `include_plan` bool to `plan_filename` string parameter
- `mobile_terminal/static/index.html` - Replaced checkbox with `<select id="challengePlanSelect">`
- `mobile_terminal/static/terminal.js` - Added loadPlans() to populate dropdown, updated preview and submit to use selected plan
- `mobile_terminal/static/styles.css` - Added .challenge-plan-row, .challenge-plan-select styles

### UI Reorganization

**Files Changed:**
- `mobile_terminal/static/index.html` - Moved planBtn to header-right, removed terminal-header with "Terminal" title
- `mobile_terminal/static/terminal.js` - Unified refresh button logic (view-aware), updated plan visibility to use classList

### Queue Persistence & Reconnect

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Added QUEUE_STORAGE_PREFIX, persistQueue(), loadPersistedQueue(), reconcileQueues(), grace period for overlay
- `mobile_terminal/server.py` - Added /api/queue/reconcile endpoint

### Log View Fixes

**Files Changed:**
- `mobile_terminal/static/terminal.js` - Added lastLogContentHash, simpleHash(), fixed refresh to force userAtBottom=true, added plan mode tool logging

### WebSocket Stability

**Files Changed:**
- `mobile_terminal/server.py` - Added connection_closed flag, checked before all WebSocket sends

**Commits:**
- `fdd2221` Add plan selector dropdown to Challenge modal
- `28f10c5` Fix WebSocket send-after-close errors during disconnect
- `2265e4f` Move Plan button to top bar, remove terminal header, unify refresh
- `695d849` Improve terminal refresh: add toast feedback and WebSocket reconnect
- `9723043` Fix log stuck after plan mode and improve change detection
- `f6a988b` Add Dev Preview tab for Replit-like service preview
- `d7267fc` Add queue/log reconciliation after reconnect
- `6e62cbe` Add grace period for reconnect overlay to reduce flicker
- `6bd4a18` Add client-side queue persistence with reconciliation
- `bf4a501` Add persistent command queue with idempotency
- `7547299` Add plan-repo linking, multi-project warnings, transcript rotation
- `3c63e6c` Add active plan access to Challenge and log view

---

## 2026-01-23: Git Revert Dirty Directory Handling

**Goal:** Transform "Working directory not clean" error into a choice point with stash and discard options.

### Server Changes (server.py)

**New Endpoints:**
- `POST /api/git/stash/push` - Create auto-stash with timestamp message
- `GET /api/git/stash/list` - List all stashes
- `POST /api/git/stash/apply` - Apply stash (safer than pop, preserves stash on conflict)
- `POST /api/git/stash/drop` - Drop a stash
- `POST /api/git/discard` - Reset hard + optional git clean -fd

**Modified:**
- `/api/rollback/git/status` - Now returns `untracked_files` count separately from `dirty_files`

### Client Changes (terminal.js)

**New State:**
- `pendingDirtyAction` - Tracks what action triggered dirty modal ('dry-run' or 'revert')
- `lastStashRef` - Tracks stash created during revert flow for post-revert management

**New Functions:**
- `showDirtyChoiceModal(action)` / `hideDirtyChoiceModal()` - Choice modal management
- `handleStashChoice()` - Stash and continue with pending action
- `showDiscardConfirmModal()` / `hideDiscardConfirmModal()` - 2-step discard confirmation
- `handleDiscardConfirm()` - Discard (with optional untracked) and continue
- `historyExecuteRevertWithStash()` - Revert variant that shows stash result modal
- `showStashResultModal()` / `hideStashResultModal()` - Post-revert stash management
- `applyStash()` / `dropStash()` - Stash management actions
- `setupDirtyChoiceModals()` - Wire up all modal event handlers

**Modified:**
- `updateRevertButtonState()` - Buttons now enabled even when dirty (dirty handled by modal)
- `historyDryRunRevert()` - Added dirty check at start, shows choice modal if needed
- `historyExecuteRevert()` - Added dirty check at start, shows choice modal if needed

### UI Changes (index.html)

**New Modals:**
- `#dirtyChoiceModal` - Choice between stash and discard
- `#discardConfirmModal` - 2-step discard confirmation with untracked checkbox
- `#stashResultModal` - Post-revert stash management (apply/drop)

### Style Changes (styles.css)

**New Classes:**
- `.dirty-choice-modal` - Base modal (centered, dark overlay)
- `.dirty-choice-content` - Modal content container
- `.dirty-choice-header` - Header with warning/success variants
- `.dirty-choice-btn` - Large touch-friendly option buttons
- `.discard-confirm-*` - Discard confirmation specific styles
- `.stash-result-*` - Stash result modal styles

### UX Flow

1. User clicks Dry Run or Revert
2. If dirty, shows choice modal:
   - "Stash changes and continue" - Safe, preserves work
   - "Discard all changes" - Shows confirmation with optional untracked removal
   - Cancel - Returns to previous state
3. If stash chosen and revert succeeds, shows stash management modal:
   - Apply Stash - Restore changes
   - Drop Stash - Discard stash
   - Close - Keep stash for later

**Files Changed:**
- `mobile_terminal/server.py` - 5 new endpoints, git status enhancement
- `mobile_terminal/static/terminal.js` - Dirty handling functions, modal setup
- `mobile_terminal/static/index.html` - 3 new modals
- `mobile_terminal/static/styles.css` - Modal styling

**Design Decisions:**
- Use `git stash apply` not `pop` (safer - stash preserved if apply fails)
- Discard requires explicit 2-step confirmation
- `git clean -fd` opt-in via checkbox (unchecked by default)
- Buttons enabled when dirty (modal intercepts, not disabled state)

---

## 2026-01-25: New Window in Repo Feature

**Goal:** Allow creating new tmux windows in configured repos directly from the mobile overlay.

### Server Changes (server.py)

**New Endpoints:**
- `POST /api/window/new` - Create new tmux window in repo's session
  - JSON body: `{repo_label, window_name?, auto_start_claude?}`
  - Validates repo_label against config.repos
  - Sanitizes window_name: `[a-zA-Z0-9_.-]`, max 50 chars
  - Adds random suffix (secrets.token_hex(2)) for uniqueness
  - Uses `tmux new-window -t {session} -n {name} -c {path} -P -F "format"`
  - Optional auto_start_claude sends "claude" + Enter after 300ms
  - Returns: `{success, target_id, pane_id, window_name, session, repo_label, path}`
- `GET /api/repos` - List configured repos with path existence status

**Security:**
- Only repos from `.mobile-terminal.yaml` allowed (no arbitrary paths)
- Server-side sanitization of window name
- subprocess list args only (no shell=True)
- Timeouts on all subprocess calls
- Audit log: `window_create` action with full details

### Client Changes (terminal.js)

**New State:**
- `availableRepos` - Cached list of repos from /api/repos
- DOM refs for new window modal elements

**New Functions:**
- `loadRepos()` - Fetch available repos from server
- `showNewWindowModal()` - Display modal, populate repo selector
- `hideNewWindowModal()` - Close modal
- `createNewWindow()` - Make API call, handle response, auto-select new target
- `setupNewWindowModal()` - Wire up event handlers

**Modified:**
- `renderTargetDropdown()` - Added "+ New Window in Repo..." option when repos configured

### UI Changes (index.html)

**New Modal:**
- `#newWindowModal` - Modal with repo selector, window name input, auto-start checkbox

### Style Changes (styles.css)

**New Classes:**
- `.new-window-modal` - Modal container
- `.new-window-content`, `.new-window-header`, `.new-window-body`, `.new-window-footer`
- `.new-window-select`, `.new-window-input`, `.new-window-btn`
- `.new-window-field.checkbox` - Checkbox field styling
- `.target-option.new-window` - "+ New Window" option in dropdown

**Files Changed:**
- `mobile_terminal/server.py` - 2 new endpoints (~120 lines)
- `mobile_terminal/static/terminal.js` - Modal functions (~150 lines)
- `mobile_terminal/static/index.html` - New modal, version bumps
- `mobile_terminal/static/styles.css` - Modal styling (~100 lines)

**Commits:**
- (pending)

---

## 2026-01-26: Startup Automation, Session Recovery, Layout Hints

**Goal:** Add per-repo startup commands, Claude health monitoring with crash recovery, and layout name hints.

### Feature 1: Per-Repo Startup Automation

**config.py Changes:**
- Added `startup_command: Optional[str] = None` to Repo dataclass
- Added `startup_delay_ms: int = 300` to Repo dataclass (clamped 0..5000)
- Updated `to_dict()` serialization
- Updated `load_config()` to parse new fields

**server.py Changes:**
- `/api/repos` now returns `startup_command` and `startup_delay_ms`
- `/api/window/new` uses `repo.startup_command or "claude"` when auto_start enabled
- Validation: rejects newlines, max 200 chars
- Uses `tmux send-keys -l` (literal mode) + separate Enter
- Audit logging for startup_command_exec

### Feature 2: Session Recovery (Claude Health Monitoring)

**server.py New Endpoints:**
- `GET /api/health/claude?pane_id=...`
  - Gets pane_pid and title via `tmux display-message`
  - Scans process tree for claude-code via pgrep
  - Returns: `{pane_alive, shell_pid, claude_running, claude_pid, pane_title}`
- `POST /api/claude/start?pane_id=...`
  - Returns 409 if Claude already running
  - Gets startup_command from body or repo config
  - Uses `tmux send-keys -l` + Enter
  - Audit logging for claude_start

**terminal.js Changes:**
- Added health polling state: `claudeHealthInterval`, `lastClaudeHealth`, `claudeStartedAt`
- Added `checkClaudeHealth()` - polls `/api/health/claude` every 5s
- Only polls when `document.visibilityState === 'visible'` and `activeTarget` set
- Added `checkClaudeHealthAndShowBanner()` - re-check before showing banner
- Added `updateClaudeCrashBanner()` - show if was running, now not, with 3s debounce
- Added `respawnClaude()` - calls `/api/claude/start`
- Added `startClaudeHealthPolling()` / `stopClaudeHealthPolling()`
- `dismissedCrashPanes` Set for per-pane banner dismiss
- Visibility change listener starts/stops polling
- Target selection resets health state

**index.html Changes:**
- Added crash banner HTML after multiProjectBanner:
  - `#claudeCrashBanner`, `#claudeRespawnBtn`, `#claudeCrashDismissBtn`

**styles.css Changes:**
- Added `.claude-crash-banner` styles (red warning, 44px touch targets)
- Added `.claude-respawn-btn`, `.claude-crash-dismiss-btn`

### Feature 3: Layout Convention Hints

**server.py Changes:**
- `/api/window/new` now defaults window name to `Path(repo_path).name` instead of repo label
- This matches directory basename convention for better layout hints

**terminal.js Changes:**
- `renderTargetDropdown()` now checks if window_name matches directory name
- Compares normalized names (lowercase, without random suffix, alphanumeric only)
- Shows `<span class="target-name-hint">?</span>` badge if mismatch

**styles.css Changes:**
- Added `.target-name-hint` style (muted badge, small font)

### Files Changed
- `mobile_terminal/config.py` - Repo dataclass fields, to_dict(), load_config()
- `mobile_terminal/server.py` - 2 new endpoints, modified /api/repos, /api/window/new
- `mobile_terminal/static/terminal.js` - Health polling functions (~150 lines), DOM refs
- `mobile_terminal/static/index.html` - Crash banner HTML, version bumps
- `mobile_terminal/static/styles.css` - Crash banner and hint badge styles

### Version Bumps
- styles.css: v142 -> v143
- terminal.js: v199 -> v200

**Commits:**
- (pending)

---

## 2026-01-26: Unified Navigation Dropdown

**Goal:** Replace separate repo and target dropdowns with a single unified navigation button.

### Problem
- Two separate buttons in header: repo dropdown and target (pane) dropdown
- Confusing UX with separate controls for session switching vs pane switching
- Lock button added complexity without clear benefit

### Solution
Unified single button showing "repo • pane" format with sectioned dropdown:
1. **Current Session** - List of panes (no reconnect)
2. **Actions** - "+ New Window in Repo..."
3. **Other Sessions** - Other repos (triggers reconnect)

### CSS Changes (styles.css)
- Repurposed `.repo-btn` as unified nav button (max-width: 220px, text ellipsis)
- Added `.nav-section-header` - Section labels (uppercase, muted)
- Added `.nav-section-divider` - Horizontal separator
- Added `.nav-pane-option` - Pane items (with checkmark for active)
- Added `.nav-session-option` - Session items (with "Switch" pill badge)
- Added `.nav-action-option` - Action items (blue accent color)
- Added `.reconnect-pill` - Small badge indicating reconnect
- Hidden `.target-btn`, `.target-lock-btn`, `.target-dropdown` via `display: none !important`

### JS Changes (terminal.js)
- Added `updateNavLabel()` - Updates button to show "repo • pane" format
- Rewrote `populateRepoDropdown()` - Now renders three sections
- Made `updateTargetLabel()` and `renderTargetDropdown()` into no-ops (redirect to new functions)
- Made `updateLockUI()` a no-op (lock button hidden)
- Updated `loadTargets()` - Calls `updateNavLabel()`, removed targetBtn references
- Updated `selectTarget()` - Closes `repoDropdown` instead of `targetDropdown`
- Updated `switchRepo()` - Calls `updateNavLabel()`, removed targetBtn references
- Updated `toggleRepoDropdown()` - Shows dropdown if repos OR multiple panes exist
- Updated `setupTargetSelector()` - Removed targetBtn/targetDropdown event setup, multiProjectSelectBtn now opens unified dropdown

### Files Changed
- `mobile_terminal/static/styles.css` - Unified nav styles (~180 lines changed)
- `mobile_terminal/static/terminal.js` - Unified dropdown logic (~150 lines changed)
- `mobile_terminal/static/index.html` - Version bumps (v144, v202)

### Key Design Decisions
- Kept existing HTML element IDs (repoBtn, repoDropdown) to avoid breaking changes
- CSS hides old elements instead of removing from HTML (safer)
- Pane selection (selectTarget) does NOT trigger reconnect
- Session switching (switchRepo) DOES trigger reconnect
- Lock functionality removed (was confusing, rarely used)
- Label updates instantly, dropdown renders on open only

### Version Bumps
- styles.css: v143 -> v144
- terminal.js: v201 -> v202

**Commits:**
- (pending)

---

## 2026-01-31: Target Switch Fixes and Loading Indicators

**Goal:** Fix target switching issues causing wrong terminal content and unresponsive UI

**Files Changed:**
- `mobile_terminal/server.py` - Added get_tmux_target() helper, fixed select-pane format, added switch verification, PTY/WebSocket cleanup on switch
- `mobile_terminal/static/terminal.js` - Added loading overlay states, fixed nav label to match by cwd
- `mobile_terminal/static/index.html` - Updated SW version reference to v72
- `mobile_terminal/static/sw.js` - Bumped cache to v72

**Key Fixes:**
- tmux target format: was `session:window:pane`, now `session:window.pane`
- Nav label: was matching first repo by session, now matches by target cwd
- Added loading feedback during target switch delays

**Risks/Follow-ups:**
- Target switch verification sometimes fails (non-blocking, proceeds anyway)
- Service worker caching may require hard refresh on mobile

---

## 2026-02-17: Workspace Directory Picker

**Files changed:**
- `mobile_terminal/config.py` — Added `workspace_dirs` field, parsing, serialization, merge
- `mobile_terminal/server.py` — Added `GET /api/workspace/dirs`, modified `POST /api/window/new` for path-based creation
- `mobile_terminal/static/terminal.js` — Updated modal to show workspace dirs in optgroups, path-based creation flow

**New files:** None

**Risks/Follow-ups:**
- Workspace dirs are scanned on each modal open (no caching); should be fine for typical dir counts
- Path validation uses `relative_to()` to prevent traversal; symlinks resolve to real paths

---

## 2026-02-18: Agent-Native Features (Status Strip, Push, Artifacts)

**Goal:** Add three agent-native features: status strip for real-time phase display, push notifications for completed/crashed events, and enhanced artifacts/replay with timeline UI.

**Files changed:**
- `mobile_terminal/server.py` — Added `GET /api/status/phase` with mtime/size cache, `_detect_phase()` parser (last 8KB JSONL), `_get_git_head()` cached helper, `_try_auto_snapshot()` for event-driven snapshots, extended `push_monitor()` with idle transition (20s) and crash detection (10s debounce), updated `maybe_send_push()` for `extra_data`, added `POST /api/rollback/preview/{snap_id}/annotate`, updated `list_previews`/`get_preview`/history API for per-target scoping and lazy heavy fields
- `mobile_terminal/static/index.html` — Added status strip div after crash banner, bumped versions (terminal.js v248, styles.css v150, sw.js v115)
- `mobile_terminal/static/styles.css` — Status strip styles (24px bar, colored dots, pulse animations, action button), timeline styles (vertical connector, colored label badges, note previews, indicators)
- `mobile_terminal/static/terminal.js` — Added `updateClaudePhase()` in health poll `Promise.all`, status strip state vars, enhanced `renderHistoryList()` with timeline visual and annotation UI, SW `respawn_claude` message handler, URL `?action=respawn` param handling
- `mobile_terminal/static/sw.js` — Per-type push actions (Open for completed, Respawn+Open for crashed), respawn click handler with client postMessage and deep-link fallback

**New files:** None

**Risks/Follow-ups:**
- Phase detection reads last 8KB of JSONL + 1 tmux display-message call; cached path returns in <5ms
- Auto-snapshots are rate-limited to 1 per 30s in push_monitor; no new polling loops added
- Lazy heavy field loading means first "View" click on a snapshot triggers tmux capture-pane + JSONL read
- Annotation notes capped at 500 chars; no server-side image upload (relies on existing `/api/upload`)

---

## 2026-02-23: Agent Teams - Steps 1-3 (Discovery, Batch State, UI Grouping)

**Files Changed:**
- `mobile_terminal/server.py` - Added `team_role`/`agent_name` fields to `/api/targets`, added `_detect_phase_for_cwd()` with per-pane composite cache, added `_get_git_info_cached()` for branch/worktree/is_main, added `GET /api/team/state` batch endpoint
- `mobile_terminal/static/terminal.js` - Added `teamState` variable, `updateTeamState()` in health poll `Promise.all`, Team section in `populateRepoDropdown()` with DOM-built status dots/branch labels/permission badges
- `mobile_terminal/static/styles.css` - Added `.team-dot` (per-phase colors + pulse), `.team-phase`, `.team-branch`/`.team-branch-main` (red warning), `.team-perm-badge`, `.team-agent` layout
- `mobile_terminal/static/index.html` - Version bumps: styles.css?v=153, terminal.js?v=253

**New Files:** None

**Risks/Follow-ups:**
- `_detect_phase_for_cwd()` always parses JSONL regardless of `active` flag -- ensures permission/question detection even for stale logs
- No pgrep for team endpoint -- uses log file mtime recency (< 30s) as activity indicator instead
- Team phase cache capped at 50 entries with oldest-eviction; git info cache capped at 30 with 60s stale eviction
- Git info calls subprocess (3 git commands per uncached pane); cached 10s per cwd
- Team section skips non-team panes (window name != "leader" and not "a-*" prefix); they appear in normal "Current Session" section

---

## 2026-02-24: Leader Dispatch - Plan Routing + Message Leader

**Goal:** Select a plan file, assemble it with context + roster + orchestration instructions, write dispatch.md to leader CWD, and send a tmux instruction to the leader pane.

**Files Changed:**
- `mobile_terminal/server.py` - Added `POST /api/team/dispatch` endpoint: reads plan from `~/.claude/plans/`, discovers team panes, builds roster with git/phase info, assembles dispatch.md template (What to do now, Response contract, Roster, Plan, Background, Constraints), writes to `{leader_cwd}/.claude/dispatch.md` + archive copy, sends instruction via `tmux send-keys`
- `mobile_terminal/static/index.html` - Added `#teamDispatchBar` with plan select dropdown + Dispatch button + message input + Send button before `#teamView`, version bumps (styles.css v155, terminal.js v255)
- `mobile_terminal/static/styles.css` - Added `.team-dispatch-bar`, `.dispatch-row`, `.dispatch-plan-select`, `.leader-message-input`, `.team-card-btn.dispatch` styles (44px touch targets, accent-colored dispatch button)
- `mobile_terminal/static/terminal.js` - Added `populateDispatchPlans()` (fetches /api/plans, populates select, restores from localStorage), `dispatchToLeader()` (POST /api/team/dispatch, toast with warnings, double-tap prevention), `sendLeaderMessage()` (sends to leader via existing sendTeamInput), `updateDispatchButtonState()` (enables/disables based on plan selection + leader presence), wired into `switchToTeamView()`, `hideAllContainers()`, init section

**New Files:** None

**Risks/Follow-ups:**
- Dispatch writes files to leader CWD filesystem; relies on leader Claude reading `.claude/dispatch.md` when instructed
- `warning_main_agents` in response flags agents on main/master branch but does not block dispatch
- Plan list cached in `dispatchPlansCache` per team view visit; not auto-refreshed between visits

---

## 2026-02-26: Bottom Bar Consolidation + Vertical Space Savings

**Goal:** Unify bottom bar layout across all views, eliminate wasted vertical space from standalone strips and dead-band wrappers.

**Files Changed:**
- `mobile_terminal/static/index.html` — Removed collapseToggleWrapper (moved toggle inside controlBarsContainer), replaced view-switcher tabs with dots inside collapse-row, removed standalone agentStatusStrip, added headerPhaseIndicator to header-right, moved teamDispatchBar from above team cards to bottom bar area, version bumps (styles.css v160, terminal.js v262)
- `mobile_terminal/static/styles.css` — Added .collapse-row, .view-dots/.view-dot (replaces .view-switcher/.view-tab), .header-phase/.header-phase-label (inline status), .action-bar.collapsed, .team-dispatch-bar.collapsed; removed .collapse-toggle-wrapper, .agent-status-strip replaced with header-phase; updated .collapse-toggle-btn for inline layout
- `mobile_terminal/static/terminal.js` — Added appendStandardActionButtons() helper, unified log+terminal+team action bar buttons, toggleControlBarsCollapse() now collapses actionBar+dispatchBar too, updateViewSwitcher/setupViewSwitcher use .view-dot, updateAgentPhase targets header indicator, updateSystemStatus hides header indicator in team mode, deriveSystemSummary shows "Idle · names" when all idle, idle cards hidden when all idle (strip is sufficient), switchToTeamView shows controlBars

**New Files:** None

**Risks/Follow-ups:**
- Old .status-phase, .status-detail, .status-action-btn CSS styles are dead (no HTML/JS refs) — can be cleaned up
- View dots have 8px hit targets — may be hard to tap; consider adding padding for touch area
- phaseIdleShowHistoryTimer variable declared but no longer used after updateAgentPhase simplification


---

## 2026-04-13: Compose attachments, multiline send, log scroll-jam, candidate flood (commit da34018)

**Goal:** Fix four user-reported regressions in one batch — image attachment dropping silently from compose, multiline messages getting split mid-send, log view scrolling to the top during refresh (especially when tools collapsed), and SecondBrain backlog Suggestions tray flooded with TodoWrite items.

**Files Changed:**
- `mobile_terminal/drivers/claude.py` — `BacklogCandidateDetector._extract_candidates` now skips TodoWrite (Claude's in-session scratchpad — many items per call, rewritten constantly, flooded the tray). Only TaskCreate is extracted. Docstring updated to explain the rationale.
- `mobile_terminal/models.py` — `CandidateStore.add` enforces `MAX_PER_PROJECT = 30` cap. New adds are silently dropped once cap is reached; user must dismiss/keep before fresh ones appear.
- `mobile_terminal/helpers.py` — new `send_text_to_pane(runtime, target, text)` wraps multiline text in bracketed paste escape codes (`\x1b[200~...\x1b[201~`) so `\n` is treated as pasted content, not Enter. Single-line text passes through unchanged.
- `mobile_terminal/routers/terminal_sse.py` — `/api/terminal/text` POST handler uses `send_text_to_pane` instead of raw `runtime.send_keys(literal=True)`.
- `mobile_terminal/routers/terminal_io.py` — WS `type:"text"` handler uses `send_text_to_pane` similarly.
- `mobile_terminal/static/terminal.js`:
  - `uploadAttachment` no longer inserts the path into `composeInput.value` — the path lives only in the attachment preview card. Switched from raw `fetch` to `apiFetch`, replaced `alert()` with `showToast()`, added 0-byte file guard, surfaces HTTP status code in error messages, distinguishes network vs server errors.
  - `uploadAndInsertPath` (desktop command-bar paste/drop): same treatment — `apiFetch`, 0-byte guard, HTTP status in errors, network-error distinction. Also fixed `selectionStart === 0` falsy-fallback bug.
  - New `withAttachmentPaths(text)` helper used by `sendComposedText` and `queueComposedText` — appends any pendingAttachment paths missing from the text so the file is never silently dropped on send.
  - `renderLogEntriesChunked` rebuilt: builds new content in an off-DOM `DocumentFragment` (preserving the user's view during chunked yields), runs `applyCollapseSync(staging)` to apply collapse passes BEFORE the swap (so post-render idle callbacks can't shrink content out from under the pin-to-bottom), then atomic `replaceChildren` swap, then pin to bottom. `scrollLockUntil` set during swap to prevent transient scroll events from flipping `userAtBottom`. `LOG_MAX_ENTRIES` cap moved to BEFORE rendering via `messages.slice(-LOG_MAX_ENTRIES)`.
- `mobile_terminal/static/src/features/collapse.js` — `collapseRepeatedTools` and `applySuperCollapse` now accept optional `container` (defaults to `logContentEl`). New `applyCollapseSync(container)` export runs both passes synchronously against any container, including a `DocumentFragment`.
- `mobile_terminal/static/index.html` — script src cache-bust bumped from v=322 → v=326 (multiple bumps during session).
- `mobile_terminal/static/dist/terminal.min.js` + `.map` — rebuilt.

**New Files:** None

**Risks/Follow-ups:**
- Bracketed paste requires the receiving terminal app to support `\x1b[200~`. Claude Code, bash, zsh, vim, and tmux (paste-aware mode) all do; raw programs that don't will display the escape sequence as garbled chars. No issue observed in MTO's use cases.
- `applyCollapseSync` doesn't update `lastCollapseHash`/`lastSuperCollapseHash` (intentional — fragments are ephemeral). Click-driven re-collapse via `scheduleCollapse` still runs once after a render, but is a no-op if structure unchanged.
- `removeAttachment` (× button on preview card) only removes from `pendingAttachments`, but since the path is no longer in the textarea, that's consistent.
- Refresh still fetches the full log on every change. `renderLogEntries` rebuild is now visually smooth, but on very large logs the off-DOM build cost is real. Proper fix would be incremental fetch + append.
- TaskCreate may not be the right signal long-term. If users want NO automatic detection, the `BacklogCandidateDetector` can be disabled at server boot (currently always-on via `app.state.candidate_detector = BacklogCandidateDetector()` in `server.py:115`).
- `CandidateStore` cap is in-memory; a server restart wipes it (intended — candidates are ephemeral by design).
- Server-side changes (`helpers.py`, two routers, `claude.py`, `models.py`) require an MTO server restart to take effect. JS/HTML changes need a hard browser refresh (cache `v=326`).


---

## 2026-04-13 (later): Queue cross-pane bleed, scheduling rewrite, candidate detector off (commit 8e669f8)

**Goal:** User-reported "queue items duplicate and misallocate across sessions" + "scheduling barely works" + "backlog still accumulates many items from agent in session". Three related issues, all fixed in one batch.

**Diagnostic findings:**
- Queue WS broadcasts carried no session/pane context, so items from any pane bled into the currently-viewed queue list and localStorage.
- Two independent drain paths (client `tryDrainQueue` + server `_process_loop`) raced and could both send the same item — visible as duplicate execution in the terminal.
- Server processor only drained `app.state.active_target`, so queues for non-active panes piled up indefinitely.
- Client localStorage key used `default` fallback when no active pane; server used just the session — items written under one key, never reconciled with the other.
- `PROMPT_PATTERNS` only matched ❯ / $ / # / >>>, so zsh/fish/node/oh-my-zsh sessions never satisfied the ready gate.
- `BacklogCandidateDetector` was rescoped earlier from TodoWrite to TaskCreate, but a single observed SecondBrain orchestration session invoked TaskCreate **413 times** with **411 unique subjects** — modern Claude Code uses TaskCreate the way it used to use TodoWrite. The Suggestions tray was permanently full of "things Claude is currently doing", not "things to remember later".

**Files Changed:**
- `mobile_terminal/models.py`:
  - `CommandQueue._wakeup_event` (lazy `asyncio.Event`, bound inside `_process_loop`). `_wake()` helper. `enqueue()` and `resume()` call `_wake()` so freshly-queued items drain within ms.
  - `_parse_key` (inverse of `_queue_key`) splits "session:pane" back to (session, pane_id).
  - `_process_loop` rewritten: awaits `_wakeup_event` with 2s idle-poll fallback; iterates every queue whose session == current tmux session, drains the first safe queued item per queue, gates on `_check_ready` per-pane.
  - `_send_item` rewritten: uses `helpers.send_text_to_pane` (bracketed paste for multi-line) + explicit `runtime.send_keys(target, "Enter")`, instead of raw PTY writes via `input_queue.send`. Also stamps `app.state.last_ws_input_time` so desktop-activity detector doesn't misclassify queue writes. `queue_sent` WS payload now includes `session` + `pane_id`.
  - `PROMPT_PATTERNS` extended: `>`, `▶`, `»`, `➜`, `λ`, `\.\.\.` added to the original four.
- `mobile_terminal/routers/queue.py` — `queue_update` (add+remove) and all `queue_state` broadcasts now include `session` + `pane_id`.
- `mobile_terminal/drivers/claude.py` — `BacklogCandidateDetector.check_sync` is now a no-op. Still advances `last_log_size` so future re-enablement doesn't backfill. Class skeleton, `_extract_candidates`, `_try_add` retained dormant for future use. Docstring rewritten with v1→v2→v3 history.
- `mobile_terminal/static/src/features/queue.js`:
  - `messageTargetsCurrentView(msg)` filters WS messages by `(msg.session, msg.pane_id)` against `(ctx.currentSession, ctx.activeTarget)`. Backward-compatible with unstamped messages.
  - `getQueueStorageKey` no longer falls back to `default`; matches server keying exactly: `mto_queue_<session>` or `mto_queue_<session>:<pane>`.
  - `enqueueCommand` POST response merge clarified — server `data.item` is authoritative for status, won't revert sent→queued.
- `mobile_terminal/static/terminal.js`:
  - `setTerminalBusy` no longer schedules a 3s drain timer.
  - `tryDrainQueue` and `sendNextSafe` removed entirely (server is sole auto-drainer). `popNextQueueItemById` import dropped.
  - Manual "Run" (`sendNextUnsafe`) unchanged.
- `mobile_terminal/static/index.html` — cache-bust v=326 → v=327.
- `mobile_terminal/static/dist/terminal.min.js` + `.map` — rebuilt.

**New Files:** None

**Risks/Follow-ups:**
- "Drain all panes" is a behavior change: a queue you forgot about in pane B will fire when ready conditions are met, even if you're viewing pane A. Pause-per-pane via the Pause button still suppresses.
- `_send_item` no longer goes through `input_queue.send` — there's a tiny race window if user types raw input concurrently with a queue item firing. In practice the queue only fires when `_check_ready` sees a quiet prompt, so user can't be actively typing.
- Wider `PROMPT_PATTERNS` could false-positive on output lines ending in `>` (e.g. an HTML snippet). `BUSY_PATTERNS` and the QUIET_MS gate still guard. Watch for it; if it bites, narrow back.
- `BacklogCandidateDetector` no-op means CandidateStore never receives anything new. The store remains in `server.py:116` for code parity but is functionally dead; can be removed later if no detection signal is added.
- `reconcileQueue` race with WS messages and `pendingPrompt` stuck-state are NOT addressed in this batch — independent and lower-priority.
- Server restart required for all Python changes; hard browser refresh for `v=327` bundle.


---

## 2026-04-13 (later still): Stop+Esc, queue Previous section, docs no-cache (commit 435a804)

**Goal:** Three small unrelated UX fixes that surfaced during the same session.

**Files Changed:**
- `mobile_terminal/static/terminal.js`:
  - New `sendStopInterrupt()` helper at `terminal.js:1224-1242`. Sends `\x03` (Ctrl+C) immediately, then 100ms later sends `\x1b` (Esc). Replaces four call sites that previously sent only Ctrl+C: action-bar Stop button, tools-rail Stop action, Ctrl+C-on-empty-input shortcut, palette `sendInterrupt`. Reason: in Claude Code, Ctrl+C interrupts the agent but preserves the previously-submitted prompt in the input buffer for editing — typing after a Stop appended to the preserved text and the next submit re-sent the previous message + new content. Esc clears Claude Code's input buffer, no-op in bash/zsh, exits insert mode in vim.
- `mobile_terminal/static/src/features/queue.js`:
  - `renderQueueList` rewritten. Items now split into two sections — Active (queued/sending) and Previous (sent). Previous renders below Active, has a collapsible header with item count and a Clear button, default collapsed, expansion state persisted per-tab (`mto_queue_prev_expanded` in sessionStorage).
  - Sidebar queue count badge (`sidebarQueueCount`) now reflects only active items, not sent+active. Drawer total count (`queueCount`) keeps old behavior.
  - "All caught up" placeholder shown in Active when only Previous has items.
  - Extracted `renderQueueRowHtml(item)` so both sections share row markup. New `buildPreviousSection(items)` builds the section chrome via DOM APIs (createElement/textContent/appendChild) — body rows still use innerHTML with escapeHtml-sanitized content (same pattern as before).
  - WS `queue_sent` triggers re-render → item flips from Active to Previous.
  - Existing 60s `SENT_RETAIN_MS` auto-purge unchanged.
- `mobile_terminal/static/styles.css` — new `.queue-section-previous`, `.queue-section-header`, `.queue-section-toggle`, `.queue-section-title`, `.queue-section-count`, `.queue-section-clear`, `.queue-section-body`, `.queue-active-empty` rules.
- `mobile_terminal/static/src/features/docs.js`:
  - `const NO_CACHE = { cache: 'no-store' }` at module top.
  - Applied to every docs-tab fetch: `/api/plans`, `/api/plan?filename=`, `/api/docs/context`, `/api/docs/touch`, `/api/log/sessions`, `/api/log?session_id=`, `/api/files/tree`, `/api/file?path=`. Bypasses browser HTTP cache so the modal always shows current disk state.
  - `loadPlansTab` always fetches now (the `if (!plansCache)` short-circuit removed) — switching to another tab and back inside an open modal re-hits the server, so plan files written between tab switches show up immediately.
- `mobile_terminal/static/index.html` — cache-bust v=327 → v=330 for JS, v=238 → v=239 for CSS.
- `mobile_terminal/static/dist/terminal.min.js` + `.map` — rebuilt.

**New Files:** None

**Risks/Follow-ups:**
- Stop+Esc 100ms gap is a guess. If the interrupt is sometimes still in flight when the Esc lands, the Esc could be eaten — symptom would be "sometimes the prompt isn't cleared." Bump to 200ms or sequence via callback if observed.
- Double-tapping Stop sends two Ctrl+C+Esc pairs. Claude Code's Ctrl+C-twice-exits behavior is unchanged (pre-existing).
- Previous section's 60s auto-purge stays. If users want a longer history (e.g. last 50 sends or last hour), bump `SENT_RETAIN_MS` and add a per-render cap. Risk of "never purge" is unbounded localStorage growth; mitigated by the cap.
- Drag-reorder handle only renders for queued items; previous items can't be dragged (correct, they're already sent).
- Docs no-store costs slightly more server load on tab switching (one disk read per tab entry). Plan dir + 200-char preview is cheap; not a concern.
- File tree fetch is the heaviest of the docs no-cache fetches. If repo trees become huge, switch just that one back to caching with a manual Refresh button.
- Server restart NOT required for any of this — all client-side changes. Hard browser refresh for `v=330` bundle + `v=239` styles.


---

## 2026-04-13 (final): Compose attachment race + path-join cleanup (commit 19074c3)

**Goal:** User report — image upload combined with text in composer still fails to attach the image. Diagnosis revealed two distinct issues: a race when Send fires before the upload POST returns, and a UX confusion about what "image sent" means.

**Diagnostic findings:**
- Live test produced this terminal output: `Tes test test  /home/gcbbuilder/dev/mobile-terminal-overlay/.claude/uploads/upload-1776504678092.jpg`. The path WAS being sent, with a stray double-space.
- The user's deeper concern was that the image arrives as a path reference rather than as an inline `[Image #N]` multimodal attachment. That's a separate feature track (would require agent-specific protocol support) and the user explicitly chose to keep MTO agent-agnostic — path-as-text stays.
- Race: `uploadAttachment` is async; tapping Send before the POST returns reads `pendingAttachments` while still empty, so `withAttachmentPaths` has nothing to append, the text-only message ships, then `closeComposeModal(true)` clears state. Upload returns ~200ms later but the modal is gone — image silently lost.

**Files Changed:**
- `mobile_terminal/static/terminal.js`:
  - New module-level `let inflightUploads = []` tracking in-flight upload promises.
  - `uploadAttachment` (compose modal) and `uploadAndInsertPath` (desktop input/paste) split into outer wrapper that pushes/removes a tracker promise, and an inner `_…Inner` that does the work.
  - New helper `awaitInflightUploads()` inside setupComposeMode — `Promise.allSettled` on a snapshot, with a brief "Waiting for N upload…" toast so the user knows why there's a small delay.
  - `sendComposedText` and `queueComposedText` now `async`, await `awaitInflightUploads()` before reading `composeInput.value`.
  - `sendLogCommand` (desktop command bar) now `async`, awaits `inflightUploads` before reading `logInput.value` — same race when pasting an image then immediately hitting Enter.
  - `withAttachmentPaths` trims trailing whitespace from user text before joining paths, so `"text  "` + path becomes `"text /path"` not `"text  /path"`. Same logic catches trailing newlines.
  - Stale comment about "paths normally inserted into textarea on upload" removed (paths haven't been injected since da34018).
  - Diagnostic toast and `console.debug` block from the prior debugging round removed.
- `mobile_terminal/static/index.html` — cache-bust v=330 → v=333 (multiple bumps during diagnosis).
- `mobile_terminal/static/dist/terminal.min.js` + `.map` — rebuilt.

**New Files:** None

**Risks/Follow-ups:**
- The "Waiting for N upload…" toast is short-lived (1.5s). On slow networks the toast disappears but the await still holds — could be made progress-aware later.
- `Promise.allSettled` means a failed upload doesn't block the send — user gets whatever uploads succeeded. Right call (don't strand the message because of one bad image).
- New uploads triggered DURING the wait aren't blocked (we snapshot at await time). Matches user intent: "send what's ready now."
- `sendLogCommand` going async means callers get a Promise. Existing callers fire-and-forget — same observable behavior as before.
- "Image as multimodal attachment" (not path-as-text) remains an open feature request. Would require an agent-specific protocol mechanism. Explicitly out of scope per user direction (clean and agnostic).
- Server restart NOT required — all client-side. Hard browser refresh for `v=333` bundle.
