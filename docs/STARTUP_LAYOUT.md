# startup_layout — config-driven workspace recovery

Add a `startup_layout:` block to `config.yaml` to declare the windows
MTO should ensure exist on startup. Most useful after WSL/host restart:
instead of manually creating each window and tapping Continue per pane,
MTO recreates the layout for you.

## Example config.yaml

```yaml
session_name: claude

startup_layout:
  - window_name: overlay
    path: ~/dev/mobile-terminal-overlay
    auto_resume: true
  - window_name: secondbrain
    path: ~/dev/secondbrain
    auto_resume: true
  - window_name: geo-cv
    path: ~/dev/geo-cv
    auto_resume: false      # window appears but no agent started
```

## Per-window fields

| Field | Required | Default | Notes |
|---|---|---|---|
| `window_name` | yes | — | tmux window name. Also accepts `name:` for brevity. |
| `path` | yes | — | Absolute or `~`-prefixed cwd. Also accepts `cwd:`. |
| `auto_resume` | no | `false` | If True AND window had to be created, run `claude --continue` after a 600ms delay. Pre-existing windows are never disturbed. |

## When does it run?

1. **At MTO service startup** — applied automatically as part of the
   `auto_setup` chain, right after `ensure_tmux_setup`.
2. **On user demand** via `Restore Workspace` in the workspace nav menu
   (POSTs `/api/setup/restore`). Same logic, idempotent.

## Idempotency guarantees

- A window already in `tmux list-windows` with the same name is left
  alone. **No** cwd change, no agent restart, no Ctrl-C.
- `auto_resume` only fires for windows the function just created.
- Path that doesn't exist on disk → window skipped, error logged,
  others continue.
- Session that doesn't exist → entire layout skipped (the existing
  `ensure_tmux_setup` should have created the session first).

## Active-target fallback

If `~/.cache/mobile-overlay/active_target.txt` points at a pane that
no longer exists after restart, MTO falls back to:

1. The first window in `startup_layout`, then
2. The first existing window, then
3. None (UI shows no active target).

Stops the "land on a dead pane" UX after restart.

## Verifying

```bash
# Inspect via API:
curl -s http://127.0.0.1:8080/api/setup-status | jq '.startup_layout'

# Re-apply manually:
curl -s -X POST http://127.0.0.1:8080/api/setup/restore | jq

# Or use the "Restore Workspace" button in the workspace nav menu.
```

## Per-repo queue (sibling change)

Queue items are stored per **repo**, not per pane index. When you
kill a tmux window and a different one shifts into that index, your
queue follows the cwd, not the index. The on-disk filename is
`<session>__<sanitized-repo-path>.jsonl` (matches the backlog
keying scheme). Existing `<session>_<window>_<pane>.jsonl` files
are migrated lazily on first access.

If two panes have the same cwd, they share one queue file —
intentional, matches the backlog model.

See `.claude/plans/queue-by-repo.md` for design notes.

## Roadmap

This is v1 of the session-restore plan. v2–v6 (multi-layout, team
workspace type, save-current-as-layout, UX restructure) are documented
at `.claude/plans/session-restore-roadmap.md` and are not yet shipped.
