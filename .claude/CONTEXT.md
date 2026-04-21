# Mobile Terminal Overlay - Session Context

## Current State

- **Branch:** master
- **Stage:** Queue UX overhaul + per-client target sync + agent-memory file search live. Two open follow-ups: defensive selectTarget init, per-pane queue widget.
- **Last Updated:** 2026-04-21
- **Server Version:** v370
- **Server Start:** `./venv/bin/mobile-terminal --session claude --port 8080 --base-path /terminal --no-auth --host 0.0.0.0 &` (drop `--verbose` if still in your systemd unit — it's noisy and competes for journal I/O)

## Recent: 2026-04-20..21 — Queue UX overhaul + multi-client polish (12 commits)

Driven by real-use complaints: stale items reappearing across reconnects/devices, `[y/n]`-mistime auto-sends, queue rows too short to read, queue cross-pollution between secondbrain/MTO panes, agent recap invisible in MTO log.

| Commit | Summary |
|---|---|
| `e81f242` | **Step 6 — PTY respawn reset frame**: server drops per-pane buffers + sends `{type:'reset'}` on fresh PTY spawn; client clears matching `lastSeqByPane` keys. Closes the delta-reconnect feature. |
| `a80dcf7` | **SW push wake nudge**: sw.js push handler postMessages `sw_wake` to open clients. Backgrounded tabs whose WS died at NAT reconnect immediately on push receipt rather than waiting for tap. |
| `2f37095` | **Recap inline + pinned banner**: Opus 4.7 `system/away_summary` JSONL events now render in /api/log as `🪞 recap: ...`. New `/api/log/recap` tail-scans last 256KB for the most recent recap; banner replaces the static .claude/CONTEXT.md banner. |
| `c2b4850` | **Per-item ⚡ opt-in for auto-send**: `auto_eligible: bool = False` on QueueItem. Processor picker filters to ⚡-flagged. New POST /api/queue/auto_eligible. Default False so accidental enqueues never fire. |
| `71c1913` | **Edit-then-Queue race fix**: `consumeEditingItemRemoval` now routes through `removeQueueItem` so the v=358 `_recentlyDeleted` guard catches the race. No more "edited original reappears as duplicate". |
| `2992b09` | **⚡ overrides safe-only gate**: per-item ⚡ is the user's explicit consent — drop the `policy=='safe'` requirement from the picker. Items the user knowingly flagged auto-fire regardless of classifier verdict. |
| `93f47d8` | **Driver-phase ready gate + Pause→Hold rename**: `_check_ready` consults `driver.observe(ctx).phase` and only fires when `idle`. Stops auto-send during mid-tool-call streaming gaps. Pause button renamed "Hold" with tooltip explaining ⚡ relationship. |
| `7950bb2` | **Tombstones for queue items**: `dequeue` records `(id, removed_at)` with 24h TTL, persisted as `*.tomb.jsonl`. Re-enqueue of a tombstoned id returns synthetic `{status:'removed'}` so a different device's stale localStorage drops the item locally. Stops cross-device resurrection. |
| `7c35d3b` | **Resizable workspace sidebar (desktop)**: drag handle on right edge; bounds 240–800px; persists to localStorage. |
| `9fdb90b` | **Queue rows: title attribute**: full prompt text in `title="..."` on each row → native hover tooltip on desktop. Mobile gets the existing 2-line wrap. |
| `4fc1109` | **Agent memory in file search**: `/api/files/search` and `/api/file` accept a `memory/` virtual prefix that resolves to `~/.claude/projects/<id>/memory/`. Surfaces auto-memory files (e.g. `project_bank_description_rules.md`) that live outside the repo. |
| `5aeefad` | **Per-client target view**: `loadTargets` no longer blindly overwrites `ctx.activeTarget` from server's `data.active`. Mobile keeps its locally-chosen pane unless that pane is gone — stops desktop's pane switch from dragging mobile's queue/log view along. |

**RCAs investigated this session (no code change needed):**
- "Items keep reappearing on queue" → pre-v=357 manual sends never told the server `sent`. Cleanup script flipped 45 disk items to `sent`; v=357+ prevents recurrence.
- "X delete items reappear" → race between local filter and POST + reconcile from another tab. Fixed in v=358 (`_recentlyDeleted` guard).
- "Secondbrain queue showing in MTO pane" → desktop's pane switch leaked into mobile via the old `loadTargets` overwrite. v=370 stops it. Existing residue items (7 in pane 1:0's queue file) cleaned up by user via X button (now creates tombstones).

**Open follow-ups (concrete):**
- **Defensive selectTarget init**: on cold start, don't auto-fire `selectTarget(savedTarget, true)` if savedTarget's repo doesn't match the URL/session context. Currently any saved target gets re-applied even if the user re-opened MTO from a fresh URL targeting a different repo. ~10 LOC.
- **Per-pane queue widget**: in desktop multipane, the queue panel should follow the visible terminal column (the pane the user is reading), not the global `ctx.activeTarget`. Architectural fix for the cross-pollution discomfort. Bigger change — needs a per-column queue context.

**Files touched:**
- New: `mobile_terminal/static/index.html` (recap banner, sidebar resizer handle, ⚡ button)
- Modified: `mobile_terminal/models.py` (auto_eligible field, mark_sent, tombstones, prune), `mobile_terminal/routers/queue.py` (mark_sent + auto_eligible endpoints), `mobile_terminal/routers/files.py` (memory/ virtual prefix), `mobile_terminal/routers/logs.py` (away_summary inline + /api/log/recap), `mobile_terminal/routers/terminal_io.py` + `terminal_sse.py` (reset frame on PTY respawn, mode-switch baseline), `mobile_terminal/static/terminal.js` (queue UX, sidebar resize, lastSeq tracking, target sync), `mobile_terminal/static/src/features/queue.js` (⚡ toggle, tombstone handling, mark_sent), `mobile_terminal/static/sw.js` (push wake nudge)

## Recent: 2026-04-20 — Seq-based delta-reconnect (8 commits)

Goal: stop full-snapshot ship on every WS/SSE reconnect. Server now keeps a per-pane ring buffer of raw PTY bytes with absolute byte-seq addressing. On reconnect, client sends `?since=N`; server ships only the delta if `N` is in the buffer window, else falls back to the historical snapshot path.

| Commit | Step | Summary |
|---|---|---|
| `d42c1b3` | 1 | `mobile_terminal/pane_buffer.py` — `PaneRingBuffer` with absolute-seq addressing, brutal rollover (in-window or `None`), no `reset()` method (PTY respawn = construct a new buffer). 17 unit tests. |
| `5d5b257` | 2 | Wire buffer into `read_from_terminal` PTY drain (silent maintenance). `app.state.pane_buffers: dict[str, PaneRingBuffer]` keyed by `f"{session}:{target}"`. Cleared on tmux session switch. 5 registry tests. |
| `b71868f` | 3 | `?since=<int>` query parsing + `seq_baseline` JSON frame after `hello` on WS. Client adds no-op handler so the new frame doesn't fall through to xterm. |
| `bad7a67` | 4 | `decide_resume(pbuf, since) -> ResumeDecision` pure function with 4 modes (fresh/snapshot/caught_up/delta) + 7 tests. WS handler ships delta bytes BEFORE baseline JSON, skips clear-screen on delta path so xterm state survives. |
| `d32e805` | — | Fix: `/api/team/state${sessParam}` was using `&session=` when it was the only query param → 404. Same family as PR2 follow-up `3682de2`. |
| `5bf610f` | 7 | SSE parity in `terminal_sse.py` — same `decide_resume` decision, same baseline frame, same skip-snapshot-on-delta. Reordered before step 5 because user is on SSE (Tailscale Serve HTTP/2→WS upgrade flakiness keeps WS as second choice). |
| `5f24a7a` | — | `sync-version.js` now also rewrites `console.log('=== TERMINAL.JS vN ===')`. The line was hardcoded at v286 for ages so every deploy looked broken. |
| `6481087` | 5 | Client `lastSeqByPane` Map + conservative tracking (`_seqTrackingEnabled` only after binary in full mode). `?since=N` appended to WS+SSE reconnect URLs. Server reseeds `seq_baseline` after capture-pane snapshot on mode→full so client lastSeq matches `pane_buffer.next_seq` (tail mode doesn't ship bytes, so the connect-time baseline goes stale). In-memory only — page reload clears it (xterm is empty after reload, delta would visually corrupt). |
| `b1e8d2b` | — | Raised `MAX_QUEUE_BYTES` 200KB → 1.5MB after a real reconnect shipped a 254KB delta and overflowed the xterm write queue. Server pane buffer caps at 1MB so 1.5MB covers worst case + a couple live chunks. xterm.js drains MB/sec — the cap was backpressure, not a drain ceiling. |

**Verified live (server log):**
```
[SEQ-SSE] connect since=696009 window=[0,950914] mode=delta baseline=950914 delta_bytes=254905
```
Client said "I've seen up to seq 696009", server said "current is 950914", delta of 254905 bytes shipped — the actual NAT-recovery scenario.

**Architecture invariants:**
- Per-pane buffer keyed by `f"{session}:{target}"`. Pane switch keeps the old pane's buffer. Tmux session switch clears all (pane identities reset).
- `since < oldest_seq` OR `since > next_seq` → `None` → snapshot fallback. No partial recovery.
- Wire order on resume: delta bytes → seq_baseline → (clear-screen / capture-pane only on snapshot path).
- Client `_seqTrackingEnabled` invalidated on mode switch and pane switch. Reseeded by next `seq_baseline`.

**Known limitations (deferred):**
- **Step 6 — PTY respawn reset frame:** if the underlying PTY dies/respawns during a connection, server constructs a fresh buffer (seq=0) but never tells the client. Client could request `?since=N` against the new buffer; `decide_resume` correctly returns the snapshot path (since N > next_seq → out of window) so the conservative fallback works. A typed `{type: 'reset'}` frame would let the client clear `lastSeq` immediately rather than relying on the seq math.
- **Mode-switch race:** the `set_mode → snapshot → baseline` sequence in `terminal_io.py:466-487` is async; live PTY bytes flushing during the ~200ms window between baseline-seq read and baseline send can mis-align lastSeq by a small amount. Visual glitch on subsequent reconnect, not data corruption.
- **Pane-switch baseline:** when `selectTarget` flips active_target mid-connection, no new baseline is sent — client just invalidates tracking until next reconnect.

**Files touched:**
- New: `mobile_terminal/pane_buffer.py` (130 lines), `tests/test_pane_buffer.py` (28 tests)
- Modified: `mobile_terminal/server.py` (registry init + clear), `mobile_terminal/terminal_session.py` (PTY append), `mobile_terminal/routers/terminal_io.py` (resume decision + mode-switch reseed), `mobile_terminal/routers/terminal_sse.py` (same parity), `mobile_terminal/static/terminal.js` (lastSeq tracking + URL builders + queue cap), `scripts/sync-version.js` (TERMINAL.JS log line)

## Recent: 2026-04-13..19 — Codebase audit batch (PR1–PR6 + follow-ups)

Cross-cutting cleanup pass driven by a four-agent parallel review of
the codebase. Executed as ten focused commits across the user's
priority order. Tasks tracked in TaskList.

| PR | Commit | Summary |
|---|---|---|
| **PR1** | `d2d2169` | Single source of truth for static cache-bust: `scripts/version.txt` + `scripts/sync-version.js` propagates one integer to `index.html`, `sw.js` CACHE_NAME, and the sw.js URL inside `terminal.js`. Wired into `npm run build` (idempotent, fail-loud). Versions had drifted: JS=333, CSS=239, SW=v128, sw.js URL=v120 — all now in lock-step. |
| **PR2** | `891d5da` | Frontend `apiFetch` standardization. 96 raw `fetch(` → `apiFetch`/`ctx.apiFetch`. 118 redundant `?token=…` strips. Only intentional raw fetches remain (the two inside `apiFetch` and `fetchWithTimeout`). |
| | `3682de2` | PR2 follow-up: token-strip orphaned `&` in 6 URL templates (`/api/log`, `/api/status/phase`, `/api/rollback/git/status`, `/api/health/agent`, `/api/activity`). Fixed by switching `paneParam` to `?` prefix or rewriting via URLSearchParams. |
| **PR3a** | `185d30d` | Cache `get_repo_path_info()` (1s TTL keyed by session+target) — every router was hitting it via `deps.get_current_repo_path()` which does a tmux subprocess per call. Bust on `select_target` and `switch_repo`. |
| **PR3b** | (decision) | Keep `BacklogCandidateDetector` as a dormant skeleton — already a no-op, docstring already explains how to re-enable. No code change. |
| **PR3c** | `bd886dc` | Unify minority error response shapes. 9 routes in backlog/permissions/queue migrated from `{"status": "error"}` (HTTP 200) to `JSONResponse({"error": …}, status_code=N)` (proper HTTP errors). |
| **PR4 (1/2)** | `3e43e5e` | New `mobile_terminal/terminal_session.py` with shared `read_from_terminal`, `tail_sender`, `desktop_activity_monitor` runners + `TerminalSessionState` dataclass. ~220 lines of WS/SSE-duplicated code consolidated. `WebSocketSink.client_mode` added for sink-uniform reads. |
| **PR4 (2/2)** | `902ee8a` | Both transport handlers wired to the shared module. Transport-specific bits (WS `server_keepalive` ping/pong + `write_to_terminal`, SSE `server_keepalive` comment + POST endpoints) deliberately kept inline. |
| **PR5a** | `d9c6f30` | `registerPoller(name, fn, intervalMs)` registry + single `visibilitychange` listener pauses/resumes ALL pollers. Migrated `heartbeat`, `idle-check`, `activity-updates`. Fixed leaked nested setTimeout in idle-check (was an anonymous timer with no handle). |
| **PR5b** | `8e4836c` | Removed `isControlUnlocked` dead control flow — variable was hardcoded `true`, 13 guard sites stripped. |
| **PR5c** | `d668c0c` | Event delegation: queue/backlog/history per-render listener-bind passes replaced with single delegated handler at init. Init guards (`_xxxInitialized`) added to all 14 feature module `init*()` exports. |
| **PR5d** | `257e6e5` | Unified duplicate `formatTimeAgo` (utils.js, ms) vs `formatAgo` (permissions.js, seconds — silent unit-mismatch bug). Removed the last `alert()` (docs.js Pin failure → `showToast`). |
| **PR6** | `6948b1c` | CSS variables consolidated into `:root` (was missing/duplicated in `:root.high-contrast`). HC now overrides only what differs. `type="button"` added to all 178 unmarked `<button>` elements (defensive vs future stray `<form>` wrappers). |

### Follow-up fixes that surfaced during the audit

- **`98206ab`** cache bump after PR3a/3c
- **`8e4836c`** PR5b done out of order to clear noise
- **`ec932be`** pane-switch speedup: dropped the dead 500ms client `setTimeout` (was waiting for a WS reconnect that doesn't happen) + replaced 1-second tmux verify polling loop in `select_target` with a single subprocess call.
- **`be1c2d8`** restored `?token=` on the WebSocket URL — PR2's bulk strip caught it despite the commit message claiming it was protected. `new WebSocket()` can't set headers, breaks auth-mode deployments.
- **`09d00a4`** bounded the previous-client close on connect to 1s. Without this, a dead socket from the previous client (mobile network change) hung TCP for 30-120s, blowing past the client's 2s `HELLO_TIMEOUT` and triggering reconnect storms ending in auto-reload.
- **`e118499`** PR4 oversight: SSE `event_generator`'s PTY-died cleanup referenced `pty_died` (closure local that had been migrated to `state.pty_died`). NameError silently caught by the broad `except`, so dead PTYs were inherited by next connection. Fixed.

### Outside-this-scope ops fix

`systemd/mto.service` had `--verbose` in `ExecStart` — removed in your working-tree edit (lives alongside the rest of the systemd reorg you've been staging). To pick up: copy/symlink to `~/.config/systemd/user/` or `/etc/systemd/system/`, `daemon-reload`, `restart`. Removes most of the journal write churn that competed with the event loop.

### Known remaining work (deferred, listed for memory)

- Wider `console.error`/silent-catch sweep in feature modules (PR5d landed only the alert + duplicate helper)
- 57 `!important` uses in button states (specificity refactor)
- ~30 hardcoded hex colors that should be `var(--*)` (notably team dots in styles.css)
- Sourcemap shipping decision (`terminal.min.js.map` is ~990KB on every page load)
- Remaining `setInterval`-based pollers (connection watchdog, metrics, dev status, SSE heartbeat) not yet routed through the new `registerPoller` registry — lower frequency, lower impact

## Earlier: 2026-04-13 (final) — Compose attachment race + path-join cleanup (commit 19074c3)

User report: image upload combined with text in composer fails to attach the image.

- **Race fix**: `inflightUploads` array tracks in-flight upload promises. `sendComposedText`, `queueComposedText`, `sendLogCommand` are now `async` and await `awaitInflightUploads()` (or inline equivalent) before reading the input value. Brief "Waiting for N upload…" toast covers the wait. Fixes the case where a fast Send tap fired before `/api/upload` returned, sending text-only and silently losing the image.
- **Path-join cleanup**: `withAttachmentPaths` trims trailing whitespace from user text before appending paths, so `"text  "` + path becomes `"text /path"` not `"text  /path"`. Stale comments removed.
- **Diagnostic noise removed**: console.debug + diagnostic toast from previous round dropped now that the bug is identified.
- **Open follow-up (out of scope here)**: image-as-multimodal-attachment (vs path-as-text) would require agent-specific protocol work. Explicitly punted per user direction (keep MTO agent-agnostic).

No server restart needed; hard browser refresh for `v=333`.

## Earlier: 2026-04-13 (later still) — Stop+Esc, queue Previous, docs no-cache (commit 435a804)

Three small unrelated UX fixes. See touch-summary for detail.

- **Stop preserves "ghost" messages**: Ctrl+C in Claude Code preserves the previously-submitted prompt in the input buffer for editing — typing after a Stop appended to it and the next submit looked like the previous message was being re-sent. New `sendStopInterrupt()` sends Ctrl+C, then 100ms later Esc (clears Claude Code's input buffer; no-op in bash/zsh). Wired into all four Stop call sites.
- **Queue "Previous" section**: queue list split into Active (queued/sending) and Previous (sent). Previous is collapsible with Clear button, default collapsed, persists state per-tab. Sidebar queue badge now counts only active items. Existing 60s auto-purge of sent items still runs.
- **Docs always fetches fresh**: every docs-tab fetch uses `cache: 'no-store'`; Plans tab refetches on every entry, not only on first open — so plan files Claude writes in the background show up immediately.

No server restart needed; hard browser refresh for `v=330` JS + `v=239` CSS.

## Earlier: 2026-04-13 (later) — Queue + candidate fixes (commit 8e669f8)

Three related issues fixed together. See touch-summary for detail.

- **Queue cross-pane bleed**: WS messages (`queue_update`, `queue_state`, `queue_sent`) now stamp `session` + `pane_id`; client filters by current view. Storage key normalized to match server (`mto_queue_<session>` with no `'default'` fallback).
- **Queue scheduling rewrite**: removed client-side auto-drain (eliminates double-send race). Server is sole drainer. `CommandQueue` got an `asyncio.Event` wakeup so enqueue/resume trigger immediate drains. `_process_loop` iterates every queue belonging to the current tmux session, not just `active_target`. `_send_item` uses `send_text_to_pane` (bracketed paste) instead of raw PTY writes. `PROMPT_PATTERNS` extended for zsh/fish/node/oh-my-zsh.
- **Candidate detector off**: `BacklogCandidateDetector.check_sync` is a no-op. Modern Claude Code uses TaskCreate the way it used TodoWrite (observed 413 TaskCreate calls in one SecondBrain session). Class skeleton kept dormant for future smarter signal.

Server restart required for Python changes; hard browser refresh for `v=327` bundle.

## Earlier: 2026-04-13 — Bug-fix batch (commit da34018)

Four user-reported regressions resolved in one batch. See touch-summary for detail.

- **Compose image attachments**: path no longer injected into the textarea — lives only in the preview card. `withAttachmentPaths` re-appends paths from `pendingAttachments` at send time so the file is never lost. `apiFetch` + `showToast` + 0-byte guard make failures actually visible. Same treatment for desktop command-bar paste/drop (`uploadAndInsertPath`).
- **Multiline send via tmux**: new `helpers.send_text_to_pane` wraps multiline text in bracketed paste escape codes (`\x1b[200~...\x1b[201~`) so `\n` inside the prompt no longer triggers premature submission in Claude Code. Wired into both SSE POST and WS text handlers.
- **Log scroll-to-top jump**: `renderLogEntriesChunked` now builds in a `DocumentFragment` off-DOM, applies collapse synchronously on the fragment via new `applyCollapseSync`, then atomic `replaceChildren` swap. Final layout is settled before the pin-to-bottom — no async collapse can shrink content out from under the pin.
- **Backlog candidate flood**: `BacklogCandidateDetector` no longer scans `TodoWrite` (Claude's in-session scratchpad). Only `TaskCreate` is extracted. `CandidateStore.MAX_PER_PROJECT = 30` cap as safety valve.

Server restart required for Python changes; hard browser refresh for `v=326` bundle.

## Earlier: Permission Auto-Approval System (2026-03-20)

Graduated auto-approval for Claude Code tool permissions. Rules learned from real prompts (banner-first), managed centrally in a Permissions tab.

### Architecture
- **Policy engine**: `permission_policy.py` — PermissionPolicy class with evaluate(), risk classification, rule matching, storage, audit logging
- **Data model**: PermissionRequest, PermissionRule, PermissionDecision dataclasses in `models.py`
- **Risk classifier**: HIGH (sudo, rm -rf, git push --force) → always prompt; MEDIUM (git push, npm publish, docker); LOW (pytest, git status, reads)
- **Evaluation order**: mode → hard guard → deny rules → allow rules → fallback (prompt)
- **Rule scopes**: session (in-memory), repo (persisted), global (persisted)
- **Built-in defaults**: Read/Glob/Grep auto-allowed, git status/diff/log auto-allowed
- **Storage**: `~/.mobile-terminal/permission-policy.json` (rules) + `~/.cache/mobile-overlay/permission-audit.jsonl` (audit)

### Interception
- tail_sender (WS + SSE): policy evaluates detected permissions, injects y/n or prompts user
- push_monitor: auto-approves even when no client connected (agent doesn't get stuck)
- Enhanced permission banner: Allow | Deny | Always·Repo | Always buttons
- Auto-approval/denial toast notifications via `permission_auto` typed message

### Permissions Tab
- Mode toggle: Manual / Safe Auto / Session Auto
- Rule list grouped by scope (defaults, repo, global, session)
- Delete button for user-created rules (defaults protected)
- Recent audit log with tool/target/reason
- FAB menu entry, drawer tab, sidebar section

### API
- `GET /api/permissions/rules` → mode + rules list
- `POST /api/permissions/rules` → create rule
- `DELETE /api/permissions/rules?id=` → delete rule
- `POST /api/permissions/mode?mode=` → set mode
- `GET /api/permissions/audit?limit=` → audit entries

### Files
- `mobile_terminal/permission_policy.py` — NEW: policy engine
- `mobile_terminal/routers/permissions.py` — NEW: API endpoints
- `mobile_terminal/static/src/features/permissions.js` — NEW: tab UI
- `mobile_terminal/models.py` — PermissionRequest, PermissionRule, PermissionDecision
- `mobile_terminal/server.py` — PermissionPolicy state init + router
- `mobile_terminal/routers/terminal_io.py` — policy interception in tail_sender
- `mobile_terminal/routers/terminal_sse.py` — policy interception in tail_sender
- `mobile_terminal/routers/push.py` — policy interception in push_monitor
- `mobile_terminal/static/index.html` — banner buttons, permissions tab, FAB entry
- `mobile_terminal/static/terminal.js` — Always handlers, toast, tab wiring
- `mobile_terminal/static/styles.css` — permission tab + banner styles

## Previous: Backlog Candidate Pipeline (2026-03-19)

JSONL interception extracts work items from Claude agent output as ephemeral candidates. User promotes to backlog via Keep/Dismiss.

### Architecture
- **BacklogCandidate** (in-memory, disposable) separate from **BacklogItem** (durable, persisted)
- **CandidateStore**: in-memory, per-project, dismissed hashes remembered
- **BacklogCandidateDetector**: follows ClaudePermissionDetector pattern, reads JSONL incrementally
- **Provenance**: source (human|agent) + origin (manual|jsonl_candidate|api_report)
- Content hash dedup (MD5 of normalized summary)

### Files
- `models.py` — BacklogCandidate, CandidateStore, origin field on BacklogItem
- `drivers/claude.py` — BacklogCandidateDetector
- `server.py` — candidate_detector + candidate_store state
- `routers/logs.py` — candidate_detector.set_log_file sync
- `routers/terminal_io.py` + `terminal_sse.py` — candidate check in tail_sender
- `routers/backlog.py` — candidate API endpoints + origin param
- `static/src/features/backlog.js` — candidate tray UI
- `static/terminal.js` — WS routing for backlog_candidate
- `static/styles.css` — candidate tray styles

## Previous: Team Launcher v1 + Repo-Scoped Team UI + Smooth Dismiss (2026-03-12)

### Team Launcher
- **Templates**: 6 built-in templates in `mobile_terminal/team_templates.py` (solo_reviewer, research_implement, feature_delivery, bug_hunt, review_swarm, refactor_validate)
- **Backend**: `mobile_terminal/routers/team_launcher.py` — phased pipeline (validate → check_branch → create_windows → start_claude → await_ready → inject_prompts → save_roles → dispatch), dry_run support, per-agent results
- **Frontend**: `mobile_terminal/static/src/features/team-launcher.js` — two-step modal (plan+goal+template → roster+options), plan dropdown auto-fills goal
- **Modal HTML**: Added to index.html after stashResultModal
- **FAB menu**: "Launch Team" item added (data-action="launchTeam")
- **Empty state**: "No team running" + "Launch Team" CTA in team cards view
- **Router registered** in server.py

### Repo-Scoped Team UI
- `isTeamInCurrentRepo()` helper in terminal.js compares active target CWD against team member CWDs
- Team dot in view switcher only shown when team is in current repo
- Team section in nav dropdown only shown when team is in current repo
- Team panes always hidden from "Current Session" pane list (even when viewing other repo)
- Sidebar team count badge scoped to current repo
- `renderTeamCards()` shows "Team active in **[repo]**" when viewing different repo
- Added `cwd` field to team state API response (`routers/team.py`)

### Smooth Team Dismiss
- "Dismiss Team" button in team cards view (right-aligned, subtle)
- Backend: parallel `asyncio.gather` kills all windows simultaneously (was sequential with 2s wait)
- Frontend: stops team card refresh timer before kill, renders empty state in one shot after
- Crash detection suppressed when `ctx.teamState` is null (team just dismissed)

### Log Stability After Send
- `sendLogCommand()` no longer does hard log reset (was `logLoaded=false` + `loadLogContent()`)
- Now invalidates hash + calls `refreshLogContent()` which preserves scroll and previous context

### Files Changed
- `mobile_terminal/team_templates.py` — NEW: templates, role prompts, validation
- `mobile_terminal/routers/team_launcher.py` — NEW: launch/templates/kill endpoints
- `mobile_terminal/routers/team.py` — added `cwd` field to team state entries
- `mobile_terminal/server.py` — registered team_launcher router
- `mobile_terminal/static/src/features/team-launcher.js` — NEW: launcher modal logic
- `mobile_terminal/static/src/features/team.js` — import launcher, empty state CTA, dismiss button, repo scoping
- `mobile_terminal/static/terminal.js` — isTeamInCurrentRepo(), FAB handler, repo-scoped team UI, crash suppress, log send fix
- `mobile_terminal/static/styles.css` — launcher modal + dismiss button styles
- `mobile_terminal/static/index.html` — modal HTML, FAB item, version bump (v280/v200)

## Previous: Header Reorganization + Pane Quick-Switcher + Context Pill (2026-03-10)

### Header Layout
- **header-left:** Connection indicator (green dot), context pill (% context remaining), phase indicator (dot + label)
- **header-right:** Sidebar toggle, Docs, last activity, refresh, push, repo switcher (button + dropdown)
- Repo dropdown now opens downward from header (was upward from collapse row)

### Pane Quick-Switcher (Collapse Row)
- Collapse row replaced repo switcher with pane quick-switcher buttons
- Shows all panes in current session as compact buttons, active pane highlighted
- Tapping a pane calls `selectTarget()` for instant switching
- Refreshes after `loadTargets()` and `selectTarget()`
- On desktop: collapse row shown via CSS override even when control bars hidden

### Context Usage Pill
- `extractContextUsage()` parses "XX% context left" from raw terminal capture (before `cleanTerminalOutput` strips it)
- Context pill in header-left with color coding: green (>40%), yellow (20-40%), red (<20%)

### Desktop CSS
- `.app.desktop-multipane .control-bars-container.hidden` overridden to `display: flex` so collapse row visible
- Input/control/role bars force-hidden on desktop (only collapse row shows)
- Collapse toggle hidden on desktop (not needed)

### Files Changed
- `mobile_terminal/static/index.html` — Header restructured with header-left/header-right divs, repo btn moved to header, collapse row uses `recentRepos` div
- `mobile_terminal/static/styles.css` — header-right position relative, repo-dropdown opens downward, recent-repos styles, desktop overrides for control bars, context pill styles
- `mobile_terminal/static/terminal.js` — `extractContextUsage()`, `populateRecentRepos()` (pane-based), context pill update in poll cycle
- `mobile_terminal/static/dist/terminal.min.js` — Rebuilt bundle

## Recent: Codebase Cleanup Batches 1-4 (2026-03-04)

### Batch 1: Bug Fixes (`fc5ce74`)
- Fixed `get_bounded_snapshot()` unbound variable (`content = ""` before loop)
- Added missing `import sys` for `do_restart()` execv fallback
- Fixed XSS in challenge modal (escape AI response before innerHTML)
- Fixed XSS in attachment paths, queue list, and error handlers

### Batch 2A: Dead JS Removal (`630102d`) — -943 lines
- Removed transcript system (fetchTranscript, renderTranscript, etc.)
- Removed legacy git UI (loadGitCommits, showGitCommitList, dryRunRevert, etc.)
- Removed unreachable setupHybridView (~215 lines)
- Removed 10 no-op stubs and their call sites
- Removed dead DOM lookups and globals
- Preserved: cleanTerminalOutput, stripAnsi, renderPreviewList

### Batch 2B: Dead CSS Removal (`0129b0d`) — -1321 lines
- Removed search modal, FAB layout, repo/target option dropdowns
- Removed plan modal + plan linking, queue drawer, view toggle
- Removed context/touch containers, status phase, thinking indicators
- Replaced .log-entry with `display: none !important` (historical renders)
- Fixed duplicate @keyframes, .icon-btn, .transcript-content .prompt
- Consolidated .icon-btn blocks, removed redundant #repoLabel

### Batch 2C: Server + HTML Cleanup (`027d94d`) — -53 lines
- Removed unused imports: atexit, deque, find_claude_log_file
- Removed 30 redundant local imports (subprocess, json, time, re, os)
- Replaced `import time as _time` / `import json as json_mod` aliases
- Removed duplicate /restart endpoint (kept /api/restart with debounce)
- Removed 4 dead HTML element IDs, migrated onclick to addEventListener
- Normalized self-closing `<input />` tags

### Batch 3: CSS Variables (`77db1c4`) — +15 variables
- Added 15 missing CSS custom properties to :root
- Aliases: --error, --accent-red, --accent-blue, --accent-green, --border-color
- New: --text-muted, --accent-cyan, --accent-magenta, --cyan, --magenta, --info
- New: --bg-hover, --bg-elevated, --surface-2, --surface-3, --font-mono

### Batch 4: Consolidate Duplicates (`fc41d90`) — -107 lines
- Consolidated 3 escapeHtml() → 1 (string-replacement version)
- Removed duplicate formatBytes() → use formatFileSize()
- Removed duplicate formatRelativeTime() → use formatTimeAgo()
- Extracted get_project_id() helper, replaced 7 inline computations
- Extracted _read_claude_file() helper for 4 doc endpoints

### Batches 5-8 (completed)
- **Batch 5:** State leaks — Set overflow, URL revocation, GitOpLock TOCTOU
- **Batch 6:** Efficiency — PR cache, AbortController, global error boundary
- **Batch 7:** Accessibility — aria-labels on icon buttons and form inputs
- **Batch 8:** Mobile touch targets — 44px minimum on small buttons

## Architecture Review Phase 1: Security (2026-03-04)

### Commit 1: XSS Sink Fixes (`586beab`)
- Added single-quote escaping and String() coercion to escapeHtml()
- Fixed 22 innerHTML XSS vectors: populateRepoDropdown, renderTreeNode,
  renderFileTree, openFileInModal, createLogCard, challenge modal,
  plan refs, process/terminate, history timeline, queue/snapshot lists
- All server/tmux/filesystem data now goes through escapeHtml()

### Commit 2: Auth Consolidation (`409f709`) — -163 lines
- Replaced 97 copy-pasted token checks with verify_token() FastAPI dependency
- Token accepted via Authorization: Bearer, X-MTO-Token header, or query param
- Client sends X-MTO-Token header via apiHeaders() helper
- Default bind: 127.0.0.1 (was 0.0.0.0), default auth: ON (was OFF)
- Added --no-auth CLI flag, startup warning for 0.0.0.0 without auth
- secrets.compare_digest() for constant-time token comparison

### Commit 3: CSP + Script Loading (`680b850`)
- Added Content-Security-Policy middleware: self + cdn.jsdelivr.net for scripts
- Added X-Content-Type-Options, X-Frame-Options, Referrer-Policy headers
- Added defer to 4 CDN scripts + terminal.js (unblocks DOM parsing)
- Moved inline SW registration to terminal.js (no more unsafe-inline scripts)

### Commit 4: Async Subprocess (`e043fad`)
- Added run_subprocess() helper wrapping subprocess.run in run_in_executor
- Converted 81 subprocess.run calls in async functions
- Covers: tmux, git, pgrep, gh, systemctl, tailscale commands
- 13 sync-context calls remain (tmux session helpers, git info cache)

### Total Impact
- ~2400 lines of dead code removed
- 3 bugs fixed (unbound var, missing import, XSS)
- 15 CSS variables defined
- 5 duplicate utility groups consolidated

---

## Recent: MCP Server + Plugin Management (2026-03-04)

### Feature: MCP Server CRUD
New "MCP" drawer tab for managing Claude Code's global MCP servers (`~/.claude/settings.json` → `mcpServers` key) from mobile. Add, edit, and remove servers without SSH.

- **Atomic writes:** Write to `.tmp`, `fsync`, backup to `.bak`, then `rename` over original
- **Corruption guard:** `load_claude_settings()` returns `(dict, error)` tuple; endpoints refuse to write (409) when file is corrupt
- **Input validation:** Name regex `[a-zA-Z0-9._-]{1,64}`, non-empty command, args total < 4KB
- **Upsert:** POST overwrites if name exists, returns `updated: true/false`
- **Edit mode:** Tap Edit on a server card → form populates, name disabled, Save Changes / Cancel
- **Shell-like arg parsing:** `shellSplit()` handles single/double quotes

### Feature: Plugin Toggle Management
Same tab also manages Claude Code plugins (`enabledPlugins` key). Toggle switches for installed plugins, add new plugins by ID.

- `GET /api/plugins` reads both `enabledPlugins` from settings.json and installed plugins from `~/.claude/plugins/installed_plugins.json`
- `POST /api/plugins/toggle` enables/disables plugins via atomic settings write

### Feature: Agent Restart with --resume
After config changes, banner offers restart options to pick up new MCP servers/plugins.

- **Restart Pane:** Stops and restarts agent in current pane only
- **Restart All:** Enumerates all tmux sessions via `/api/tmux/sessions`, queries `/api/team/state` per session, finds all running agents, restarts all in parallel
- **Safe stop:** Sends Ctrl-C, polls `/api/health/agent` every 500ms for 10s, sends second Ctrl-C if needed
- **Resume:** Starts with `claude --resume` to preserve conversation context
- **Confirmation dialog:** `confirm()` before both restart modes
- **Smart banner:** Different messages based on whether agent is running

### Fix: Tab Strip Overflow on Mobile
Added `overflow-x: auto` to `.rollback-tabs` and `flex-shrink: 0` to `.rollback-tab` so MCP tab doesn't push off-screen.

### Files Changed
- `mobile_terminal/server.py` — `load_claude_settings()` / `save_claude_settings()` atomic helpers, `MCP_NAME_RE` validation, GET/POST/DELETE `/api/mcp-servers`, GET/POST `/api/plugins`, audit logging
- `mobile_terminal/static/index.html` — MCP tab button, `#mcpTabContent` panel with restart banner, plugins section, server list, add/edit form
- `mobile_terminal/static/terminal.js` — `shellSplit()`, `loadMcpServers()`, `renderMcpServerCard()`, `editMcpServer()`, `cancelMcpEdit()`, `addMcpServer()`, `removeMcpServer()`, `loadPlugins()`, `togglePlugin()`, `addPlugin()`, `stopAgentInPane()`, `startAgentWithResume()`, `mcpRestartAgents()`, `mcpSetDirty()`, tab wiring + event delegation
- `mobile_terminal/static/styles.css` — MCP tab styles (~140 lines), plugin toggle switch, tab strip overflow fix
- `mobile_terminal/static/sw.js` — Cache version bump

---

## Recent: Queue Insert-to-Edit + Per-Pane Scoping (2026-03-03)

### Feature: Insert-to-Edit
Replaced "Send Next" button with "Insert" — pops next queued item into the input box for editing before sending. Tapping any queued item also inserts it. Queue items are removed on insert; user sends via normal input flow.

### Feature: Reorder Queue Items
Added up/down arrow buttons (▲/▼) per queued item. Swap locally + POST to /api/queue/reorder. First queued item hides ▲, last hides ▼.

### Feature: Per-Pane Queue Scoping
Queue is now keyed by `session:pane_id` instead of just `session`. Server CommandQueue uses `_queue_key()` helper. All queue API endpoints accept optional `pane_id` param. Client includes `activeTarget` in all queue API calls and localStorage keys. On target switch: saves current queue, loads new pane's queue, reconciles with server.

### Files Changed
- `mobile_terminal/server.py` — `_queue_key()` helper, pane_id param on all CommandQueue methods and API endpoints, `_process_loop` uses `active_target`
- `mobile_terminal/static/terminal.js` — `insertNextToInput()`, `reorderQueueItem()`, reorder buttons in `renderQueueList()`, tap-to-insert, per-pane `getQueueStorageKey()`, pane_id in all API calls, queue save/load on target switch
- `mobile_terminal/static/styles.css` — `.queue-item[data-status="queued"]` cursor/active styles, `.queue-item-reorder` and `.queue-reorder-btn` styles
- `mobile_terminal/static/index.html` — "Send Next" → "Insert", version bumps

---

## Recent: Prompt Banner "Other" Option + Heuristic Fix (2026-03-02)

### Feature: "Other" Textarea Input
When Claude presents multi-option prompts (AskUserQuestion), tapping "Other" now shows a blank textarea instead of sending the choice number immediately. On Send: choice number → Ctrl+U (clear prefill) → feedback text. Back returns to choice buttons with zero terminal I/O.

### Feature: Many-Choices Vertical Layout
Prompts with 4+ choices now stack vertically (full-width buttons) instead of wrapping in a fixed-height area.

### Fix: Heuristic Prompt False Positives
Method 2 (numbered list detection) now rejects lists containing markdown formatting (`**`, backtick) — these are documentation/summaries, not prompt choices.

### Files Changed
- `mobile_terminal/static/terminal.js` — Modified `showPromptBanner()`, `setupPromptBannerHandlers()`, added `showOtherInput()`, `restorePromptChoices()`, `sendOtherFeedback()`, added markdown guard in heuristic detection
- `mobile_terminal/static/styles.css` — Added `.many-choices` layout, `.prompt-other-*` textarea styles

---

## Previous: Desktop Responsive Multi-Pane Layout (2026-03-02)

### Feature: Desktop Multi-Pane Layout (>=1024px)
On desktop screens, Team sidebar + Log main area shown simultaneously via CSS grid. Terminal docks as bottom panel. Mobile behavior unchanged. Includes density toggle (comfortable/compact/ultra), team search + filter, keyboard shortcuts (j/k/a/d/Enter/1/2/3), hover actions, agent selection.

### Key Architecture
- `uiMode` global ('mobile-single' | 'desktop-multipane') is the single guard for all desktop behavior
- `checkDesktopLayout()` runs on load + debounced resize (250ms)
- `#viewsContainer` wraps all views — flex column on mobile, CSS grid on desktop
- `#teamCardsContainer` separates team header from card content (stable DOM, no re-render of header)
- Singleflight guards on both refresh loops prevent request pileup

## Previous: Bottom Bar Consolidation (2026-02-26)

### Feature: Unified Bottom Bars + Vertical Space Savings
All three views (log/terminal/team) now share identical bottom bar layout. Redundant idle indicators consolidated into system status strip.

### Changes
- **Collapse toggle moved inside controlBarsContainer** — eliminates 32px dead-band wrapper
- **Collapse-row:** dots (view switcher) + chevron toggle, inline at top of control bars
- **View switcher → dots:** Compact dot indicators replace text tabs, next to collapse button
- **Collapse hides all bottom bars:** shortcut bar + dispatch bar + action bar
- **All views share same action bar:** •••, Select, Challenge, Compose (via `appendStandardActionButtons()`)
- **Agent status strip → header inline:** Phase dot + label as compact pill in header-right (saves 28px)
- **System status strip hidden when all idle? No** — shows condensed "Idle · leader, a-1, a-2"
- **Idle cards hidden when all idle** — strip is sufficient, no big card blocks
- **Dispatch bar moved to bottom** — between controlBars and actionBar, only visible in team view

### DOM Order (bottom bars)
```
terminalBlock         ← input + send
controlBarsContainer  ← collapse-row (dots + ▲) + inputBar + controlBar + roleBar
teamDispatchBar       ← plan select + dispatch, message + send (team only)
actionBar             ← •••, Select, Challenge, Compose (all views)
```

### Legacy viewBar
- Hidden with `style="display:none"`, buttons still exist for JS references (drawersBtn, selectCopyBtn, challengeBtn, composeBtn)
- Action bar delegates clicks to legacy buttons via `.click()`

---

## Previous: Urgency-Driven Mobile Layout (2026-02-25)

### Feature: Urgency-Driven Mobile Layout (Phase 1-3)
Reimplemented mobile UI with information hierarchy: status strip → view switcher → sectioned team cards → contextual action bar. UIState mapping layer: `deriveUIState(agent)` pure function + `deriveSystemSummary(agents, uiStates)`. Section classification: permission/question → Attention, working/planning/waiting → Active, idle → Idle. SACRED RULE: "Needs Attention" is for actionable-by-human states ONLY.

---

## Previous: Leader Dispatch (2026-02-24)

### Feature: Plan Routing + Message Leader
Select a plan file, assemble dispatch.md with context + roster + orchestration instructions, write to leader CWD, and send tmux instruction.

---

## Previous: Agent Teams - Discovery, Batch State, UI Grouping (2026-02-23)

### Feature: Team Discovery + Batch State + UI Grouping
Team-aware views for Claude agent teams using tmux window naming conventions: `leader` = team leader, `a-*` = agents.

#### Step 1: Team Discovery in /api/targets
- `team_role` ("leader" | "agent" | null) and `agent_name` fields on each target
- `has_team` boolean on response

#### Step 2: GET /api/team/state Batch Endpoint
- **`_detect_phase_for_cwd()`** - Per-pane phase detection using explicit cwd, no pgrep
- **Activity:** Log file mtime recency (< 30s) as informational `active` flag, NOT a gate
- **Always parses:** JSONL + pane_title checked regardless of activity (catches long waits)
- **`waiting_reason`:** "permission" (Signal Detection Pending) | "question" (AskUserQuestion) | null
- **`permission_tool`/`permission_target`:** Extracted from last tool_use when waiting for permission
- **`_get_git_info_cached()`:** Branch, worktree detection (.git is file), is_main warning; 10s cache per cwd
- **Cache:** Composite key `session:target:cwd:log_path`, mtime+size invalidation, 50 entry cap

#### Step 3: UI Grouping in Nav Dropdown
- **Team section** before "Current Session" with status dots, branch labels, permission badges
- **Status dots:** Color per phase (green=working, blue=planning, orange=waiting, purple=agent, grey=idle)
- **Branch labels:** Truncated at 20 chars, red for main/master, worktree tooltip
- **Permission badge:** Orange `!` circle only for permission waits (not questions)
- **Non-team panes** filtered out of Team section, shown in normal "Current Session"
- **Polling:** `updateTeamState()` in health poll `Promise.all`, re-renders dropdown if visible

#### spawn-team.sh
Shell script for safe team creation with guardrails:
- Session validation, main-branch protection (auto-creates feature branch)
- Per-agent branches (`<base>-leader`, `<base>-a-eval`, etc.)
- Duplicate window detection, `--kill` cleanup mode
- Starts `claude --worktree` in each window

### Files Changed
- `mobile_terminal/server.py` - team_role/agent_name in /api/targets, _detect_phase_for_cwd(), _get_git_info_cached(), GET /api/team/state
- `mobile_terminal/static/terminal.js` - teamState variable, updateTeamState(), Team section in populateRepoDropdown()
- `mobile_terminal/static/styles.css` - .team-dot, .team-phase, .team-branch, .team-perm-badge, .team-agent layout
- `mobile_terminal/static/index.html` - Version bumps: styles.css?v=153, terminal.js?v=253
- `spawn-team.sh` - Team creation script

---

## Previous: Agent-Native Features (2026-02-18)

### Status Strip
- `GET /api/status/phase` with `(log_path, mtime, size)` cache key
- Phase detection: last 8KB JSONL + pane_title + tool_use scan
- Action buttons: "Approve" (waiting), "History" (idle transition)

### Push Completed/Crashed
- Idle transition detection (20s), crash detection (10s debounce)
- Per-type push notification actions

### Artifacts & Replay
- Event-driven auto-capture (1 per 30s), minimal payload, lazy heavy fields
- Annotation, per-target scoping, timeline UI

---

## Previous: Workspace Directory Picker (2026-02-17)

### Feature
The "New Window in Repo" modal now also shows directories under configurable `workspace_dirs` (e.g. `~/dev/`), so you can open a tmux window in any project without pre-configuring each one in YAML.

### Changes
- `config.py`: Added `workspace_dirs: List[str]` field, parsing, serialization, merge
- `server.py`: Added `GET /api/workspace/dirs` endpoint (scans workspace dirs, excludes hidden + already-configured repos, limit 200)
- `server.py`: Modified `POST /api/window/new` to accept `path` as alternative to `repo_label` (validates path is under a workspace_dir)
- `terminal.js`: Updated `showNewWindowModal()` to fetch workspace dirs and render `<optgroup>` sections
- `terminal.js`: Updated `createNewWindow()` to parse `repo:` vs `dir:` value prefixes
- `terminal.js`: Updated `hasRepos` checks to also consider `workspace_dirs`

### Config
```yaml
workspace_dirs:
  - "~/dev"
```

---

## Previous: Terminal Responsiveness + Garbled Output (2026-02-04)

### Problem
1. Terminal takes ~30-90s to become responsive on mobile
2. Terminal view shows garbled output at start (tail/log view is OK)

### NEW FIX (v245): ANSI-Safe Boundary Detection

**Root cause found:** Client-side `enqueueSplit()` was splitting incoming binary data
at arbitrary 2KB boundaries, which could split ANSI escape sequences (like `\x1b[38;2;255;128;64m`)
in the middle. This causes xterm.js to receive incomplete sequences, resulting in garbled output.

**Fix implemented:**
1. Added `findSafeBoundary()` function that scans backwards from cut position to find safe split points
2. Detects incomplete ANSI sequences by looking for ESC (0x1B) without terminator
3. Also avoids splitting UTF-8 continuation bytes (0x80-0xBF)
4. Increased CHUNK_SIZE from 2KB to 8KB to reduce splitting frequency

**Diagnostic logging added:**
- `=== TERMINAL.JS v245 EPOCH SYSTEM LOADED ===` at script start
- `[v245] WebSocket connected (mode=..., epoch=...)` on socket open
- `[MODE] v245 Switching ... -> ... (epoch=...)` on mode changes
- `[TERMINAL] v245 Writing ... bytes (epoch=..., first chunk)` on first terminal write

### Files Modified (v245)
- `mobile_terminal/static/terminal.js`:
  - Added `findSafeBoundary()` function for ANSI-safe chunking
  - Modified `enqueueSplit()` to use safe boundaries
  - Increased CHUNK_SIZE to 8192 (8KB)
  - Added distinctive v245 diagnostic logging
- `mobile_terminal/static/index.html` - v245
- `mobile_terminal/static/sw.js` - v108

### Verification Steps for User
1. Open browser dev tools (Console tab)
2. Reload the page
3. Should see: `=== TERMINAL.JS v245 EPOCH SYSTEM LOADED ===`
4. Switch to Terminal view
5. Should see: `[MODE] v245 Switching tail -> full (epoch=1)`
6. If you DON'T see these logs, clear site data and unregister Service Worker

### Previous Fixes (v244) - Still Active
- Mode epoch cancellation system
- Mode-gated writes (all data gated behind `outputMode === 'full'`)
- Bytes-only pipeline (no string splitting)

---

## Recent: Target Switch Fixes and Loading Indicators (2026-01-31)

### Root Cause Fix
- **Bug:** tmux target format was wrong - used `session:window:pane` instead of `session:window.pane`
- **Fix:** Added `get_tmux_target()` helper to convert `window:pane` (e.g., "2:0") to tmux format `session:window.pane` (e.g., "claude:2.0")
- Fixed in: select-pane, capture-pane, WebSocket handler, /api/refresh, /api/terminal/capture

### Target Switch Improvements
- `/api/target/select` now blocking with verification (polls up to 500ms)
- Closes PTY and kills child process on target switch
- Closes WebSocket with code 4003 to force client reconnect
- Added `target_epoch` counter for cache invalidation
- Clears output buffer on switch

### Loading Indicators
- "Switching to target..." - shown immediately when user taps target
- "Connected, loading..." - shown when WebSocket connects
- "Loading terminal..." - shown after hello handshake
- Overlay hides when terminal data arrives

### Nav Label Fix
- **Bug:** Label always showed first matching repo (by session), ignoring which pane was selected
- **Fix:** Now matches repos by target's `cwd` path, falls back to directory name if no repo matches

---

## Recent: Startup Automation, Session Recovery, Layout Hints (2026-01-26)

### Per-Repo Startup Automation
- Added `startup_command` and `startup_delay_ms` fields to Repo config
- `/api/repos` now returns startup settings
- `/api/window/new` uses repo's startup_command when auto_start enabled
- Validation: no newlines, max 200 chars
- Uses `tmux send-keys -l` (literal mode) + separate Enter for safety

### Session Recovery (Claude Health Monitoring)
- `GET /api/health/claude?pane_id=...` - Check if Claude is running
  - Returns: `{pane_alive, shell_pid, claude_running, claude_pid, pane_title}`
  - Scans process tree for claude-code process
- `POST /api/claude/start?pane_id=...` - Start Claude in pane
  - Returns 409 if already running
  - Uses repo's startup_command if available
- Client-side health polling every 5s (when visible)
- Crash banner with respawn button after 3s debounce
- Per-pane dismiss tracking

### Layout Convention Hints
- New windows default to directory basename (not repo label)
- Target dropdown shows "?" hint badge when window name doesn't match directory
- Helps identify mismatched layouts

---

## Recent: New Window in Repo Feature (2026-01-25)

### Feature
Create new tmux windows in configured repos directly from the mobile overlay, with optional auto-start Claude.

### New Endpoints
- `POST /api/window/new` - Create new tmux window in repo's session
  - Body: `{repo_label, window_name?, auto_start_claude?}`
  - Returns: `{success, target_id, pane_id, window_name, session}`
- `GET /api/repos` - List configured repos with path existence status

### Security
- Only repos defined in `.mobile-terminal.yaml` are allowed
- Server-side window name sanitization: `[a-zA-Z0-9_.-]`, max 50 chars
- Random suffix added to prevent collisions
- All subprocess calls use list args (no shell=True)
- Actions audit logged

### Client UI
- "+ New Window in Repo..." option in target dropdown
- Modal with repo selector, window name input, auto-start Claude checkbox
- Auto-select new target after creation (with retry logic)

---

## Recent: Server Auto-Restart on Resume (2026-01-25)

### Problem
When PWA resumes from background, server code changes aren't picked up until manual restart. This breaks the mobile workflow.

### Solution
Safe server restart endpoint that PWA calls automatically on failed reconnect.

### Endpoint: POST /api/restart
- **Auth:** Same token mechanism as other APIs
- **Response:** 202 `{"status": "restarting"}` on success
- **Debounce:** 429 `{"error": "Restart too soon", "retry_after": N}` if within 30s
- **Logging:** Logs client IP and restart mechanism used

### Restart Mechanism Priority
1. **systemd (preferred):** `systemctl --user restart mobile-terminal.service`
   - Checks if service is active first
   - Clean restart, maintains socket activation benefits
2. **execv (fallback):** `os.execv(sys.executable, [sys.executable] + sys.argv)`
   - Used when systemd unavailable
   - Not compatible with uvicorn --reload or multiple workers

### Client Behavior (terminal.js)
1. On `visibilitychange` → visible: attempt normal reconnect
2. If still disconnected after 2.5s: call POST /api/restart
3. Client-side cooldown: 60s between restart attempts
4. On 202 response: wait 1.5s, then reconnect

### Safety Features
- Server-side 30s debounce prevents restart loops
- Client-side 60s cooldown provides additional protection
- tmux/Claude sessions completely unaffected (only web server restarts)
- Restart happens in background task after response flushes

## Active Work: Session-to-Log Mapping + Manual Selection (Implemented)

### Problem
When multiple Claude Code instances run in the same directory (different tmux sessions), the `/api/log` endpoint picks the most recently modified `.jsonl` file, which may not match the session being viewed.

### Solution Implemented
Target-to-log mapping using `pane_id` as the key with pinning support:

```python
app.state.target_log_mapping = {}  # Maps pane_id -> {"path": str, "pinned": bool}
```

**Auto-detection strategy** in `detect_target_log_file()`:
1. Check cached/pinned mapping first (pinned = user manually selected)
2. Find Claude process in target pane via `pgrep -P {pane_pid} -x claude`
3. Get process start time from `/proc/{pid}/stat`
4. **Strategy A: Debug file correlation** (most reliable)
   - Check `~/.claude/debug/*.txt` files
   - Match debug file first-line timestamp to Claude process start time (within 5s)
   - Debug file UUID = log file UUID
5. Strategy B: Match against first entry timestamp in each `.jsonl` file (within 60s)
6. Fallback: Most recently modified file not assigned elsewhere
7. Cache the mapping for future requests (pinned=False)

**Manual session selection** (2026-01-25):
- `GET /api/log/sessions` - List all log files with metadata (preview, timestamps, size)
- `POST /api/log/select?session_id=UUID` - Pin a specific log to current target
- `POST /api/log/unpin` - Revert to auto-detection

**Key fix (2026-01-25):**
- Fixed tmux target format: `session:window.pane` (e.g., `claude:0.0`) not `session:window:pane`

**Cache invalidation:**
- On target selection change (`/api/target/select`) - only if not pinned
- On session switch (`/api/switch`)
- On explicit unpin (`/api/log/unpin`)

### Files Modified
- `mobile_terminal/server.py`: Added `detect_target_log_file()`, `target_log_mapping` state, session selector endpoints, docs endpoints

## Docs Modal (2026-01-25)

Unified document viewer accessible via "Docs" button in header. Replaces the old Plan button.

### Tabs
| Tab | Content | Features |
|-----|---------|----------|
| **Plans** | ~/.claude/plans/*.md | Dropdown selector to pick plan |
| **Context** | .claude/CONTEXT.md | Read-only view |
| **Touch** | .claude/touch-summary.md | Read-only view |
| **Sessions** | Other session logs | Read-only viewer with Back button |

### New Endpoints
- `GET /api/docs/context` - Read CONTEXT.md from target repo
- `GET /api/docs/touch` - Read touch-summary.md from target repo
- `GET /api/log?session_id=xxx` - Load specific session log (for Sessions tab)

### Files Modified
- `mobile_terminal/server.py` - Added `/api/docs/context`, `/api/docs/touch`, `session_id` param to `/api/log`
- `mobile_terminal/static/index.html` - Changed planBtn to docsBtn, planModal to docsModal with tabs
- `mobile_terminal/static/styles.css` - Added docs-modal and docs-tab styles
- `mobile_terminal/static/terminal.js` - Replaced setupPlanButton with setupDocsButton, tab switching logic

## Objective

Build a mobile-optimized terminal overlay for accessing tmux sessions from phones/tablets.

## Completed

- [x] Project structure and pyproject.toml
- [x] FastAPI server with WebSocket tmux relay
- [x] Auth disabled by default (Tailscale-friendly), opt-in via --require-token
- [x] Static files (HTML, CSS, JS)
- [x] xterm.js integration
- [x] Control bars with collapse toggle
- [x] Control keys bar (^B, ^C, ^D, ^Z, ^L, ^A, ^E, ^W, ^U, ^K, ^R, ^O, Tab, Esc)
- [x] Quick bar (arrows, numbers, y/n/enter, slash)
- [x] Role prefixes from config
- [x] Compose modal for predictive text / speech-to-text
- [x] Image upload in compose modal (saves to .claude/uploads/)
- [x] Repo switching dropdown
- [x] File search modal
- [x] Select mode with tap-to-select
- [x] Copy to clipboard functionality
- [x] PWA support (service worker, manifest, standalone mode)
- [x] systemd service file for auto-start
- [x] WebSocket resilience (handles malformed messages)
- [x] Transcript view with syntax highlighting
- [x] tmux capture-pane history on connect
- [x] Terminal block UI with active prompt display
- [x] Input box sync with tmux (Up/Down/Tab sync via refresh button)
- [x] Client-side key debounce (150ms)
- [x] Always-on controls (lock removed, collapse toggle in tab bar)
- [x] Unified collapse (view bar + control bars collapse together)
- [x] Streamlined header (refresh in header, no working indicator)
- [x] V2: Git PR-aware status + safer revert UX
- [x] V2: Process management (terminate/respawn/status)
- [x] V2: Preview filters + turn-based auto-snapshots
- [x] V2: Runner with allowlisted quick commands
- [x] V2: Connection resilience (hello handshake, watchdog, PTY death detection)
- [x] Target Selector: Explicit pane selection for multi-project workflows
- [x] Docs Modal: Unified viewer for Plans, Context, Touch, and Sessions
- [x] New Window in Repo: Create tmux windows from mobile with auto-start Claude option

## Recent Changes (2026-01-23) - Git Revert Dirty Handling

### Safe Revert with Dirty Directory
- **Dirty choice modal** - When repo is dirty, shows choice instead of blocking:
  - "Stash changes and continue" (safe, preserves work)
  - "Discard all changes" (requires 2-step confirmation)
- **Post-revert stash management** - After revert with stash, shows Apply/Drop options
- **Untracked files checkbox** - Opt-in removal of untracked files on discard
- **Safer stash handling** - Uses `git stash apply` not `pop` (preserves stash on conflict)

### New Endpoints
- `POST /api/git/stash/push` - Auto-stash with timestamp
- `GET /api/git/stash/list` - List stashes
- `POST /api/git/stash/apply` - Apply stash (non-destructive)
- `POST /api/git/stash/drop` - Drop stash
- `POST /api/git/discard` - Reset hard + optional clean

---

## Recent Changes (2026-01-23) - Dev Preview & UX Polish

### Dev Preview Tab (Replit-like)
- **New "Dev" tab** in drawer for previewing running services
- Configuration via `preview.config.json` per repo
- Health status indicators (green/red dots)
- Start/Stop/Restart controls send commands to PTY
- Open/Copy buttons for Tailscale URLs
- Iframe preview with sandbox

### Challenge Improvements
- **Plan selector dropdown** - pick any plan from ~/.claude/plans
- Replaces "Include active plan" checkbox
- Preview updates when plan selection changes

### UI Reorganization
- **Plan button moved to top bar** (visible when plan exists)
- **Removed "Terminal" header** from terminal view
- **Unified refresh button** - refreshes log or terminal based on active view
- Refresh shows toast feedback and reconnects WebSocket if needed

### Queue Persistence
- **Client-side queue persistence** via localStorage
- Queue survives page reload and reconnects
- Reconciliation on reconnect (dedup, reorder)
- Grace period for reconnect overlay (reduces flicker)

### Log View Fixes
- **Fixed log stuck after plan mode** - force render on refresh
- **Content hash comparison** for change detection
- Plan mode tool logging (EnterPlanMode, ExitPlanMode, Task, TodoWrite)

### WebSocket Stability
- **Send-after-close fix** - prevents errors during disconnect
- Connection closed flag checked before all sends

## Recent Changes (2026-01-21) - Safety & UX

### Target Safety Checks (v0.2.0)
- **Server-side validation:** All action endpoints now validate session+pane_id
- **Client sends params:** Every action API call includes `getTargetParams()`
- **409 Conflict:** Server returns expected vs received if mismatch
- **Lock toggle:** Target locked by default (prevents auto-follow of tmux focus)

### Log Scroll Fix
- **Problem:** Scroll jumped to random positions when new content arrived while reading
- **Solution:** Don't re-render while user is scrolling - store pending content
- **Indicator:** "↓ New content" shows when updates are pending
- **Render on demand:** Content renders when user scrolls to bottom or clicks indicator

### Drawer Backdrop
- Tap outside drawer to close (semi-transparent overlay)
- Backdrop appears when drawer opens, hides when closed

## Recent Changes (2026-01-20) - Target Selector

### Multi-Project Target Selector
- **Problem solved:** Working with multiple projects in different tmux windows now works correctly
- `/api/targets` - Lists all panes/windows in session with their working directories
- `/api/target/select` - Set the active target pane for all operations
- `get_current_repo_path()` updated to prioritize explicitly selected target
- Header dropdown shows project name with pane ID, path in dropdown options
- Selecting a target refreshes log view and context to show that project's data

### How It Works
1. Open tmux session with multiple windows (e.g., `ops:geo-cv`, `ops:cuisina`, `ops:studie`)
2. Each window can be in a different project directory
3. Use target dropdown to select which window's project to work with
4. All operations (git, log, context) use the selected target's directory

## Recent Changes (2026-01-20) - V2 Features

### Git v2
- PR-aware status banner: Shows associated PR number/link when branch has an open PR (uses `gh pr view`)
- Safer revert UX: Revert button disabled until dry-run passes; dry-run validates each commit separately

### Process v2
- `/api/process/terminate` - SIGTERM first, SIGKILL fallback (force=true)
- `/api/process/respawn` - Recreate PTY session
- `/api/process/status` - Check process health
- Process tab in drawer with visual status banner and action buttons

### Preview v2
- Filter buttons: All | User | Tool | Done | Error
- Turn detection: Auto-captures snapshots when log changes with `tool_call`, `claude_done`, or `error` labels
- Friendly labels: Display names like "User", "Tool", "Done" instead of raw codes

### Runner v2
- Allowlisted commands: Build, Test, Lint, Format, Typecheck, Dev Server
- Quick command buttons: Grid layout with icons in Runner tab
- Custom command input: Run arbitrary commands with basic safety checks
- `/api/runner/commands`, `/api/runner/execute`, `/api/runner/custom`

### Connection Resilience
- Server hello handshake on WebSocket connect (client expects within 2s)
- PTY death detection with close code 4500
- Connection watchdog for stuck states
- Hard refresh button after 3 failed reconnects

---

## Portability: Using with Other Projects

### Solution: Target Selector (Implemented)

The overlay now supports explicit target selection via the header dropdown:

1. **Resolution priority** in `get_current_repo_path()`:
   - Explicit target selection (`app.state.active_target`)
   - Configured repos (from config.yaml)
   - project_root config option
   - Active tmux pane cwd (fallback)
   - Server cwd (last resort)

2. **Target dropdown** in header shows all panes with their project directories

3. **Selecting a target** refreshes all context-dependent views (log, git, context)

### Config-based mapping (still supported)

Add to `~/.config/mobile-terminal/config.yaml`:
```yaml
repos:
  - session: claude
    path: /home/gbons/dev/mobile-terminal-overlay
  - session: myproject
    path: /home/gbons/dev/myproject
```

### Feature Portability Status

| Feature | Status | Notes |
|---------|--------|-------|
| Terminal I/O | Works | Pure relay, no path dependency |
| Log file | Works | Uses selected target's project directory |
| Git ops | Works | Uses `get_current_repo_path()` with target selection |
| File search | Works | Uses `get_current_repo_path()` with target selection |
| Uploads | Works | Goes to `.claude/uploads/` relative to selected target |
| Context/Touch | Works | Reads from selected target's `.claude/` directory |

---

## Architecture: Session vs Target

```
┌─────────────────────────────────────────────────────────────┐
│  SESSION: "ops"  (tmux session name, set via --session)     │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ Window 0    │  │ Window 1    │  │ Window 2    │         │
│  │ name: geo   │  │ name: cuisi │  │ name: studie│         │
│  │             │  │             │  │             │         │
│  │ Pane 0:0    │  │ Pane 1:0    │  │ Pane 2:0    │         │
│  │ cwd: geo-cv │  │ cwd: cuisina│  │ cwd: studie │         │
│  │   ← TARGET  │  │             │  │             │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘
                           ↓
              get_current_repo_path() returns
              /home/gbons/dev/geo-cv
                           ↓
         Git ops, log view, context.md all use this path
```

### Session
- **What:** tmux session to connect terminal I/O to
- **Set:** At server start via `mobile-terminal --session ops`
- **Scope:** All windows/panes in that session are available as targets
- **Terminal typing:** Goes to the **active pane** in tmux (wherever cursor is)

### Target
- **What:** Which pane's working directory to use for file operations
- **Set:** Via header dropdown in the UI
- **Scope:** Determines `get_current_repo_path()` result
- **Used by:** Git status, log file, CONTEXT.md, file search

### Key Distinction

| Operation | Follows | Behavior |
|-----------|---------|----------|
| Terminal I/O | tmux active pane | Follows focus (typing goes where cursor is) |
| File operations | Selected target | Stays locked (git/log/context use explicit selection) |

This prevents the "cd elsewhere mid-task" footgun: even if you switch tmux focus or cd to another directory, git/log/context still use your explicitly selected target.

### Safety Features
- **Target strip:** Shows current working directory below header
- **Fallback warning:** Yellow highlight when using auto-detected path
- **localStorage:** Target persists across page reloads
- **409 on missing:** Server returns error if selected pane no longer exists

---

## Recent Changes (2026-01-18)

### Header Simplification
- Removed "Log" title from log view
- Moved refresh button (↻) to header - refreshes log AND syncs input box
- Removed sync button from input box (refresh handles both)
- Removed "working..." indicator (redundant with activity timestamp)
- Header now: `[repo] ... [activity] [refresh] [search] [connection dot]`

### Unified Collapse Behavior
- View bar (Select/Stop/Challenge/Compose) now collapses with control bars
- Single collapse toggle controls all bottom bars

### UI Simplification
- Removed lock button from header (controls always enabled)
- Moved collapse toggle to tab indicator section (next to dots)
- Empty input box Enter sends `'\r'` (confirms prompts)
- Control bar Enter kept as "stateless confirm" (ignores input box state)
- Fixed aggressive filter that hid Claude's interactive options

### Input Box Sync with tmux Terminal
- Atomic send: `command + '\r'` as single write (no race conditions)
- Up/Down arrows sync terminal history to input box
- Tab completion syncs completed text back to input
- Client-side 150ms debounce on key sends (Ctrl+C bypasses for immediate interrupt)
- Multi-prompt pattern detection: Claude `❯`, bash `$`, zsh, Python `>>>`, Node `>`

### Terminal Block UI
- Active prompt display shows last 20 lines of terminal
- Clean terminal output in log view
- Auto-suggest commands from terminal prompt

---

## Changes (2026-01-15)

### UI Simplification
- Unified action bar (viewBar) always visible: Select | Copy | Scroll | Refresh | Compose
- Removed duplicate Select/Copy buttons from inputBar
- Compose button now auto-unlocks control mode
- Removed bottomBar (merged into viewBar)

### Transcript View
- Added Term/Log toggle in header to switch views
- Transcript fetches clean history via `tmux capture-pane -p -J -S -10000`
- Syntax highlighting: prompts (green), paths (blue), flags (yellow), strings (green)
- Visual separation: command lines have blue border, output is indented/muted
- Search with match highlighting
- Proper flexbox scrolling (min-height: 0 fix)

### History & Scrolling
- WebSocket connect now uses `tmux capture-pane` for clean history (not raw buffer)
- Added `/api/transcript` endpoint for full transcript fetch
- Added `/api/refresh` endpoint for manual terminal refresh
- Fixed auto-scroll hijacking (only scroll to bottom if user was already there)
- Fixed touchend preventDefault blocking scroll on transcript

### Endpoints
| Endpoint | Purpose |
|----------|---------|
| `GET /api/transcript` | Returns 10000 lines of tmux history |
| `GET /api/refresh` | Returns 5000 lines for terminal refresh |

## Known Issues / In Testing

- Terminal native scroll doesn't work with tmux (use Scroll button for tmux copy mode)
- Transcript view is the recommended way to read history

## Key Files

| File | Purpose |
|------|---------|
| `mobile_terminal/server.py` | FastAPI + WebSocket endpoint + capture-pane APIs |
| `mobile_terminal/config.py` | Config dataclass + YAML loading |
| `mobile_terminal/cli.py` | CLI entrypoint |
| `mobile_terminal/static/terminal.js` | xterm.js + WebSocket + transcript view |
| `mobile_terminal/static/styles.css` | Mobile-first CSS with transcript styling |
| `mobile_terminal/static/sw.js` | Service worker for PWA |
| `mobile-terminal.service` | systemd unit file |

## tmux Configuration

For best scrollback in Transcript view:
```bash
# Add to ~/.tmux.conf
set -g history-limit 50000
```

## Deployment

```bash
# Manual start (auth disabled by default for Tailscale)
mobile-terminal --session claude --port 8765

# With token auth (for non-Tailscale networks)
mobile-terminal --session claude --require-token

# systemd (auto-start on boot)
sudo cp mobile-terminal.service /etc/systemd/system/
sudo systemctl enable --now mobile-terminal
```

## Access

- HTTP: `http://<hostname>:8765/`
- PWA: Install from Chrome menu for standalone mode
