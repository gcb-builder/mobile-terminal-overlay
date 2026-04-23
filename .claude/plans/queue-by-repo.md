# Queue: key by repo path, not pane index

Drawn up 2026-04-23 after a pane-index drift bug: killing a tmux
window shifted indices, causing the queue file at `claude_3_0.jsonl`
(which held secondbrain items written when secondbrain was at
window index 3) to be served to the geo-cv pane (now at window
index 3).

The backlog already keys by repo path (uses `_sanitize_project`
in models.py). It survived the same drift unscathed — proof that
the design works.

## Problem in one paragraph

`CommandQueue._queue_key(session, pane_id)` returns
`f"{session}:{pane_id}"`. `pane_id` is the tmux `window_index:pane_index`
which is ephemeral. When windows are killed/created, indices
renumber, and a queue file written under the old index now serves
a different pane.

## Target design

Queue keyed by `(session, repo_path)`. The server resolves the
client-supplied `pane_id` to its current `pane_current_path`, derives
a stable filename via the same `_sanitize_project()` the backlog
uses, and reads/writes that file regardless of which pane index
maps to that repo at any moment.

Wire protocol unchanged — the client still POSTs `pane_id=3:0`. The
resolution is internal.

## Schema / file layout

Old layout (current):
```
~/.cache/mobile-overlay/queue/
  claude_1_0.jsonl              ← pane 1:0
  claude_3_0.jsonl              ← pane 3:0
  claude_3_0.sent.jsonl
  claude_3_0.tomb.jsonl
```

New layout:
```
~/.cache/mobile-overlay/queue/
  claude__home_gcbbuilder_dev_mobile-terminal-overlay.jsonl
  claude__home_gcbbuilder_dev_secondbrain.jsonl
  claude__home_gcbbuilder_dev_secondbrain.sent.jsonl
  claude__home_gcbbuilder_dev_secondbrain.tomb.jsonl
```

Filename pattern: `<session>__<sanitized-cwd>.jsonl` (double underscore
between session and path so the parser can split unambiguously).

## Migration

On first access of a pane via `/api/queue/list?pane_id=X`:
1. Resolve pane → cwd via `tmux display-message -t X -p '#{pane_current_path}'` (cached 30s).
2. Derive new repo-key filename.
3. If new file doesn't exist AND old `claude_<X>.jsonl` exists, **rename** old → new (along with `.sent.jsonl` / `.tomb.jsonl` siblings).
4. Read the (possibly just-renamed) new file.

Migration is implicit, opportunistic, and idempotent. No one-shot
script needed; data moves the first time each pane is touched.

Edge cases:
- Old file exists, but pane's cwd doesn't match any historical
  pane that wrote it: still migrate to current cwd's repo-key. Worst
  case = items go to the wrong repo on first migration if the user
  changed cwd of a pane between runs. Acceptable — rare, and
  recoverable by deletion.
- Pane has no cwd / cwd unreachable: fall back to legacy pane-id
  key for that request. Logged as a warning.

## Edge case: same repo, multiple panes

If two panes both have `cwd=~/dev/secondbrain`, they share one
queue file. Items enqueued via either pane appear in both views.
Auto-send fires once (server's processor binds to one pane at a
time via `app.state.active_target`). This is actually a
**feature** — matches the backlog model, and means switching panes
doesn't lose your queue.

## Implementation steps

1. **`_get_pane_cwd(session, pane_id)`** in helpers.py — tmux subprocess + 30s in-memory cache.
2. **`_repo_key(session, cwd)`** in models.py — wraps `_sanitize_project`.
3. **Hybrid `_queue_key()`** — accepts `pane_id` OR `repo_key`, prefers repo_key when both available.
4. **`get_queue_file()` / `get_sent_ids_file()` / `get_tomb_file()`** — accept the new key form.
5. **`migrate_pane_to_repo(session, pane_id, repo_key)`** — atomic rename of `.jsonl`, `.sent.jsonl`, `.tomb.jsonl` if old files exist and new ones don't.
6. **Public methods** (`enqueue`, `dequeue`, `mark_sent`, `set_auto_eligible`, `list_items`, `pause`, `resume`, `flush`) — translate `pane_id` arg to repo_key once, then pass repo_key everywhere internally.
7. **`_process_loop`** — iterate repo_keys. For each repo with queued items, look up the currently-active pane in that session whose cwd matches; send to it.
8. **Migration trigger**: in `_get_queue` (the first-access lazy loader), call `migrate_pane_to_repo()` before loading.
9. **Tests** — repo-key derivation, migration of all 3 sidecar files, two panes same repo share queue, fallback when cwd unreachable.
10. **Docs** — update `STARTUP_LAYOUT.md` with a note about per-repo queue.

## What stays out

- One-shot migration script. Rely on lazy migration on first access.
- Server-side reconcile across already-running clients during migration. They'll see a momentary refetch on next `/api/queue/list`.
- Multi-session handling. Each session keeps its own keyspace
  (the `claude:` prefix in the filename). No cross-session sharing.

## Effort

~80 LOC + ~50 LOC tests. Single commit, deployable.

## Risk

- Per-`/api/queue/list` adds one tmux subprocess for the cwd
  lookup. Cached 30s per pane → <0.1ms p50.
- Migration race: two requests for the same pane simultaneously
  try to rename. Use `Path.replace()` (atomic on Linux) and treat
  EEXIST gracefully.
- Old code paths reading raw `claude_W_P.jsonl` filenames (e.g.
  ad-hoc scripts) break. Search for hardcoded names: only the
  helpers in models.py construct them. Safe.
