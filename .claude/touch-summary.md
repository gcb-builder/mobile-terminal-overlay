# Touch Summary Log

Append-only log of implementation batches.

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
