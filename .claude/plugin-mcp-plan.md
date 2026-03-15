# Plugin & MCP Server Installation Plan

## Current State

**Enabled plugins:** `frontend-design`, `/simplify`, `/fetch-docs`
**MCP servers configured:** none

## Recommended MCP Servers (priority order)

### 1. playwright (MCP server)
- **Why:** CuisinaPlatform and secondbrain both have heavy Playwright e2e suites. The centralized `playwright` CLI (v1.58.2, on PATH) runs pre-written test scripts. The MCP server (`@playwright/mcp`) is complementary — it gives Claude structured tools (`browser_navigate`, `browser_click`, `browser_screenshot`, `browser_fill`) to *drive a browser interactively*. Useful for visual verification of UI changes without writing test files.
- **Config:** `npx @playwright/mcp@latest` (stdio)

### 2. github
- **Why:** All 4 active repos are on GitHub. PR management, issue tracking, code search without leaving the terminal.
- **Config:** HTTP, requires `GITHUB_PERSONAL_ACCESS_TOKEN`

### 3. stripe
- **Why:** CuisinaPlatform uses Stripe heavily (`@stripe/stripe-js`, `stripe`, `svix` webhooks).
- **Config:** HTTP, `https://mcp.stripe.com`

### 4. context7
- **Why:** Documentation lookup across the varied stack (PyTorch, Drizzle, Radix, FastAPI). Pulls version-specific docs from source repos.
- **Config:** `npx -y @upstash/context7-mcp` (stdio)

## Recommended Plugins

### 1. typescript-lsp
- **Why:** CuisinaPlatform (React/Express/Drizzle) and secondbrain (monorepo) are TS-heavy. Type checking + code intelligence.
- **Already have:** `frontend-design` (complementary, not overlapping)

### 2. pyright-lsp
- **Why:** geo-cv (PyTorch) and mobile-terminal-overlay (FastAPI) are Python. Type checking and code intelligence.

## Active Sessions (context for decisions)

| Session | Project | Stack | Relevant servers/plugins |
|---------|---------|-------|--------------------------|
| secondbrain | JS/TS monorepo | Playwright, apps/packages | typescript-lsp, github, playwright |
| geo-cv | Python CV research | PyTorch, torchvision | pyright-lsp |
| CuisinaPlatform | Full-stack app | React/Radix/Tailwind, Express, Neon/Postgres, Stripe, Playwright, Drizzle | typescript-lsp, playwright, stripe, github |
| mobile-terminal-overlay | Terminal overlay | FastAPI, xterm.js, tmux | pyright-lsp, github |

## Notes

- `supabase` MCP: skip for now, CuisinaPlatform uses `@neondatabase/serverless` + `pg`, not Supabase
- Playwright CLI vs MCP: CLI runs test suites, MCP lets Claude be the browser user — complementary, not redundant
