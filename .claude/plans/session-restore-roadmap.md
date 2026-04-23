# Session restore roadmap

Goal: make WSL-restart recovery one-tap, restructure session creation
around product-shaped intents instead of tmux mechanics.

Drawn up 2026-04-23 after the manual-restore pain became real
(every reboot = 4× window-create + 4× claude --continue).

## v1 — Config-driven layout (TRACKED via TaskList #20–#27)

Single named layout in config, idempotent restoration on startup,
"Restore Workspace" button. ~145 LOC.

See live tasks #20–#27 for status. Acceptance: kill tmux session,
restart mto.service, observe configured windows reappear with
optional auto-resume.

## v2 — Multi-layout

After v1 ships and stabilizes.

### Schema

```yaml
layouts:
  default:
    description: "Daily driver"
    windows: [...]
  exploration:
    description: "Sandbox + scratch"
    windows: [...]
default_layout: default
```

Backward-compat: a top-level `startup_layout:` (v1 form) loads as
`layouts.default` automatically.

### Endpoints
- `GET /api/layouts` — list with current vs configured deltas per layout
- `POST /api/layouts/<name>/restore` — replaces the v1 endpoint, takes layout name

### UI
- Layout picker dropdown next to "Restore" button
- Cold-start card shows the `default_layout` choice with "Pick different layout" affordance

Effort: ~120 LOC. Risk: low (additive).

## v3 — Team workspace layout type

```yaml
layouts:
  team-feature-x:
    type: team
    windows:
      - name: feature-x-leader
        role: leader
        ...
      - name: feature-x-explore
        role: worker
        ...
```

`role` flows through to the driver as a hint (Claude prompt template
can read it). `team` type triggers leader-vs-worker UI grouping in
the workspace sidebar.

Effort: ~80 LOC. Risk: medium (touches team.js UI logic).

## v4 — Save current as layout

UI button "Save current as layout" → name prompt → snapshot of
existing tmux windows + cwds writes a new entry to `config.yaml`
under `layouts.<name>`.

### New endpoint
- `POST /api/layouts/snapshot?name=<x>` — reads `tmux list-windows`,
  builds a layout, atomically writes to config.yaml

### Risks
- File-write to user-edited config — preserve formatting/comments via
  ruamel.yaml (not stdlib yaml). Otherwise user's hand-edits get
  reformatted on every save.
- Concurrent edits — lockfile on config.yaml.

Effort: ~100 LOC. Risk: medium (user-config write is touchy).

## v5 — UX restructure

The cosmetic + vocabulary overhaul. Three primary surfaces:

### A. Cold-start "Restore" card
```
Previous workspace detected
Layout: default (4 windows)
3 windows missing • 1 already running
[Restore Workspace] [Review] [Start Fresh]
```

### B. "+ New" sheet (replaces "New Window" modal)
```
Start something new:
  ┌─ New Agent ────────────────┐
  │ One window with one agent  │
  │ Repo / Window name / etc.  │
  └────────────────────────────┘
  ┌─ Team Workspace ───────────┐
  │ Leader + N workers         │
  └────────────────────────────┘
  ┌─ From Saved Layout ────────┐
  │ [layout list]              │
  └────────────────────────────┘
```

### C. Per-window "Manage agent" menu
```
This window:
  ▸ Continue agent (last in this dir)
  ▸ Resume specific session...
  ▸ Start fresh agent
  ▸ Restart agent
  ─
  ▸ Close window
```

### Vocabulary cleanup
| Today | After v5 |
|---|---|
| New Window | New Agent / New Team Workspace / From Saved Layout |
| Continue Last Session | Continue agent (last in this dir) |
| Resume Session... | Resume specific session... |
| pane id (in UI) | window name |
| --continue / --resume (in UI) | hidden in tooltips |

Effort: ~250 LOC (mostly UI). Risk: medium (touches a lot of menus).

## v6 — Resumable session picker polish

Per-window menu entry "Resume specific session..." reads JSONL
sessions list (`/api/log/sessions` already exists), shows them with
timestamps + first-line previews, and on tap calls
`/api/agents/start` with explicit `session_id` for `claude --resume <id>`.

Effort: ~80 LOC. Risk: low.

## Total

| Stage | LOC | Cumulative | Visible payoff |
|---|---|---|---|
| v1 | 145 | 145 | Reboot → one tap restores everything |
| v2 | 120 | 265 | Multiple named layouts |
| v3 | 80 | 345 | Team workspaces have first-class config |
| v4 | 100 | 445 | "Save current" round-trip |
| v5 | 250 | 695 | Product-shaped UX, no tmux jargon |
| v6 | 80 | 775 | Power-user explicit resume |

## What stays out

- **tmux-resurrect / tmux-continuum**: useful for non-MTO terminal
  use, but conflicts with MTO-owned orchestration. Skip.
- **Cross-host layout sync**: each host's config.yaml is its own
  truth. If two MTO instances need the same layout, copy the file.
- **Per-layout permission rules / push config**: stays global until
  someone has a real use case.
- **Layout permissions**: any client with auth can apply any layout.
- **Layout import/export UI**: edit config.yaml.

## Decision rule

After v1 ships, **use it for a week**. If reboot recovery feels
solved and creating new sessions still feels clunky, v2–v6 in order.
If reboot is the only pain, stop at v1 and don't build the rest.
