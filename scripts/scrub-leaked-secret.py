#!/usr/bin/env python3
"""Scrub a leaked secret from MTO + Claude Code local log sinks.

Default is dry-run — reports matches per file without modifying
anything. Pass --apply to actually redact (with .scrub-bak backups).

ROTATE THE SECRET FIRST. This script only cleans local copies; the
window where the credential was valid is what hurts. Once you've
rotated, run this to remove dangling references from disk.

Usage examples
--------------
    # Dry run — see what would change
    python scripts/scrub-leaked-secret.py --literal "sk-abc...xyz"

    # Apply — backups saved as <file>.scrub-bak
    python scripts/scrub-leaked-secret.py --literal "sk-abc...xyz" --apply

    # Regex (use single quotes in shell)
    python scripts/scrub-leaked-secret.py --pattern 'sk-[A-Za-z0-9_-]{20,}' --apply

    # Also scrub a repo's .claude/uploads/ text files
    python scripts/scrub-leaked-secret.py --literal SECRET \
        --include-uploads --repo ~/dev/myrepo --apply

Sinks scanned by default
------------------------
- ~/.claude/projects/                 Claude Code session JSONLs
- ~/.cache/mobile-overlay/queue/      MTO active + sent queue files
- /tmp/mto-share-staging/             MTO Web Share Target staging

Use --sink PATH (repeatable) to add more, e.g. log files outside the
defaults.
"""
import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_SINKS = [
    "~/.claude/projects",
    "~/.cache/mobile-overlay/queue",
    "/tmp/mto-share-staging",
]

REDACT_TOKEN = "[REDACTED]"
SKIP_SUFFIXES = (".scrub-bak", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                 ".pdf", ".zip", ".tar", ".gz", ".bin", ".so", ".pyc")

# High-confidence "give-away" contexts where the captured group is very
# likely a real secret. Used by --reuse-scan to harvest candidates that
# we then grep for elsewhere (password reuse detection).
EXTRACTORS = [
    # echo 'PWD' | sudo -S ...
    (r"echo\s+['\"]([^'\"\n]{6,40})['\"]\s*\|\s*sudo", 1, "sudo pipe"),
    # --password=PWD or --password PWD
    (r"--password[= ]['\"]?([^\s'\"]{6,40})", 1, "--password arg"),
    # /password:PWD (Windows-style)
    (r"/password:['\"]?([^\s'\"]{6,40})", 1, "/password: arg"),
    # net use \\share PWD /user:X (Windows)
    (r"net\s+use\s+\S+\s+([^\s'\"]{6,40})\s+/user:", 1, "net use"),
    # mstsc /p:PWD (RDP)
    (r"mstsc\s+/p:['\"]?([^\s'\"]{6,40})", 1, "mstsc /p"),
    # postgres://user:PWD@host  (any scheme with user:pwd@)
    (r"[a-z]+://[^/\s:]+:([^@\s/'\"]{6,40})@", 1, "URL auth"),
    # PGPASSWORD=PWD, MYSQL_PWD=PWD, DB_PASSWORD=PWD-ish env-like
    (r"(?im)^(?:export\s+)?[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|KEY)\s*=\s*['\"]?([^\s'\"\n]{8,80})", 1, "env-style"),
    # KEY=VALUE in .env-style pasted blobs (lower confidence — needs env-name suffix)
    (r"(?i)\b(?:passwd|password|pwd|secret)\s*[:=]\s*['\"]([^'\"\n]{6,40})['\"]", 1, "quoted assignment"),
]

# Skip obvious placeholders so we don't chase fake values.
PLACEHOLDER_RE = re.compile(
    r"^(?:"
    r"password|passwd|pwd|secret|test|example|sample|placeholder|"
    r"todo|fixme|xxx+|yyy+|abc+|123+|foo|bar|baz|"
    r"changeme|change[._-]?me|"
    r"your[._-]?(?:password|secret|token|key|pwd)|"
    r"my[._-]?(?:password|secret|token|key|pwd)|"
    r"<[^>]+>|\$\{[^}]+\}|"
    r"true|false|null|none|undefined"
    r")$",
    re.IGNORECASE,
)


def _looks_real(value):
    """Cheap entropy + placeholder sanity check on a candidate secret."""
    if len(value) < 6:
        return False
    if PLACEHOLDER_RE.match(value):
        return False
    # Must have some character variety — pure repeats are placeholders
    # (aaaaaaa, 1234567, abcabcabc).
    if len(set(value)) < 4:
        return False
    # Drop strings that look like UUIDs (they're identifiers, not secrets)
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.I):
        return False
    return True


def extract_candidates(roots):
    """Walk all sinks, harvest candidate secrets from EXTRACTORS contexts.
    Returns dict: value → list of (Path, label) where it was first seen."""
    candidates = {}
    for f in find_files(roots):
        try:
            text = f.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, Exception):
            continue
        for pat, grp, label in EXTRACTORS:
            for m in re.finditer(pat, text):
                val = m.group(grp)
                if not _looks_real(val):
                    continue
                candidates.setdefault(val, []).append((f, label))
    return candidates


def find_reuse(value, roots, max_per_file=5):
    """Find every occurrence of `value` (literal) across all sinks.
    Returns dict: Path → list of (line_number, line_text) — capped per file."""
    needle = value.encode("utf-8")
    pat = re.compile(re.escape(value))
    out = {}
    for f in find_files(roots):
        try:
            data = f.read_bytes()
        except Exception:
            continue
        if needle not in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        hits = []
        for ln_no, ln in enumerate(text.split("\n"), start=1):
            if pat.search(ln):
                # Trim very long lines for display
                trimmed = ln if len(ln) <= 200 else ln[:197] + "…"
                hits.append((ln_no, trimmed))
                if len(hits) >= max_per_file:
                    break
        if hits:
            out[f] = hits
    return out


def _mask(s, keep=4):
    if len(s) <= keep:
        return s
    return s[:keep] + "…(len " + str(len(s)) + ")"


def run_reuse_scan(sinks, apply_redact=False):
    """Two-phase scan: extract candidates from high-confidence contexts,
    then look for the same literal value used anywhere else."""
    print("Phase 1: extracting candidate secrets from high-confidence contexts…")
    candidates = extract_candidates(sinks)
    if not candidates:
        print("  No candidates found. Nothing to cross-check.")
        return 0

    print(f"  {len(candidates)} unique candidate(s) found.")
    print()
    print("Phase 2: searching all sinks for re-use of those values…")
    print()

    total_redacted = 0
    for value, sources in sorted(candidates.items(), key=lambda kv: -len(kv[1])):
        reuse = find_reuse(value, sinks)
        # Only report when value appears in MORE than the originating
        # extraction site (i.e. some genuine reuse) OR shows up many times.
        total_hits = sum(len(v) for v in reuse.values())
        if not reuse:
            continue

        print(f"━ candidate: {_mask(value)}  (first seen via {sources[0][1]})")
        print(f"  found {total_hits} occurrence(s) across {len(reuse)} file(s):")
        for path, hits in list(reuse.items())[:8]:
            print(f"    {path}")
            for ln_no, ln in hits:
                print(f"      L{ln_no}: {ln}")
        if len(reuse) > 8:
            print(f"    … and {len(reuse) - 8} more file(s)")
        print()

        if apply_redact:
            n = 0
            for path in reuse:
                k, _ = scrub_file(path, re.compile(re.escape(value)), True)
                n += k
            total_redacted += n
            print(f"  → redacted {n} occurrence(s) of this candidate")
            print()

    if not apply_redact:
        print("Re-run with --apply to redact every occurrence of every candidate above.")
    else:
        print(f"Total redacted: {total_redacted}")
    return total_hits


def find_files(roots):
    seen = set()
    for root in roots:
        p = Path(os.path.expanduser(root))
        if not p.exists():
            continue
        if p.is_file():
            if p not in seen:
                seen.add(p)
                yield p
            continue
        for f in p.rglob("*"):
            if not f.is_file():
                continue
            if f.name.endswith(SKIP_SUFFIXES):
                continue
            if f in seen:
                continue
            seen.add(f)
            yield f


def scrub_file(path, pattern, apply_changes):
    """Return (match_count, error_or_None). Backs up before first edit."""
    try:
        data = path.read_bytes()
    except Exception as e:
        return 0, f"read failed: {e}"

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return 0, "binary"

    matches = list(pattern.finditer(text))
    if not matches:
        return 0, None

    if apply_changes:
        bak = path.with_suffix(path.suffix + ".scrub-bak")
        if not bak.exists():
            try:
                bak.write_bytes(data)
            except Exception as e:
                return 0, f"backup failed: {e}"
        try:
            new_text = pattern.sub(REDACT_TOKEN, text)
            path.write_text(new_text, encoding="utf-8")
        except Exception as e:
            return 0, f"write failed: {e}"

    return len(matches), None


def main():
    ap = argparse.ArgumentParser(
        description="Scrub a leaked secret from MTO/Claude local log sinks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage examples")[1] if "Usage examples" in (__doc__ or "") else None,
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--literal", help="Literal string to redact (auto-escaped).")
    grp.add_argument("--pattern", help="Regex pattern to redact (use shell-quoted).")
    grp.add_argument("--reuse-scan", action="store_true",
                     help="Two-phase scan: extract candidate secrets from "
                          "high-confidence contexts (echo|sudo, --password, "
                          "URL auth, env-style assignments…) then look for the "
                          "same value used elsewhere — finds password reuse "
                          "across files. Pair with --apply to redact every "
                          "occurrence of every candidate found.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes. Default is dry-run.")
    ap.add_argument("--include-uploads", action="store_true",
                    help="Also scrub .claude/uploads/ in --repo (text files only).")
    ap.add_argument("--repo", help="Repo path for --include-uploads (default: cwd).")
    ap.add_argument("--sink", action="append",
                    help="Extra sink path to scan (repeatable).")
    args = ap.parse_args()

    sinks = list(DEFAULT_SINKS)
    if args.include_uploads:
        repo = Path(args.repo or os.getcwd()).expanduser().resolve()
        sinks.append(str(repo / ".claude" / "uploads"))
    if args.sink:
        sinks.extend(args.sink)

    # --reuse-scan takes its own path — extract candidates then find reuse.
    if args.reuse_scan:
        print(f"Mode  : REUSE-SCAN  (apply={args.apply})")
        print(f"Sinks :")
        for s in sinks:
            resolved = Path(os.path.expanduser(s))
            marker = "✓" if resolved.exists() else "—"
            print(f"  {marker} {s}")
        print()
        run_reuse_scan(sinks, apply_redact=args.apply)
        return

    if args.literal:
        if len(args.literal) < 8:
            print("Refusing literal shorter than 8 chars — would over-match.", file=sys.stderr)
            sys.exit(2)
        pattern = re.compile(re.escape(args.literal))
        desc = f"literal {args.literal[:4]}…(len {len(args.literal)})"
    else:
        try:
            pattern = re.compile(args.pattern)
        except re.error as e:
            print(f"Bad regex: {e}", file=sys.stderr)
            sys.exit(2)
        desc = f"regex {args.pattern}"

    print(f"Pattern : {desc}")
    print(f"Mode    : {'APPLY (writes; backups → *.scrub-bak)' if args.apply else 'DRY-RUN (no writes)'}")
    print(f"Sinks   :")
    for s in sinks:
        resolved = Path(os.path.expanduser(s))
        marker = "✓" if resolved.exists() else "—"
        print(f"  {marker} {s}")
    print()

    total_files = 0
    affected_files = 0
    total_matches = 0
    skipped_binary = 0
    errors = 0

    for f in find_files(sinks):
        total_files += 1
        n, err = scrub_file(f, pattern, args.apply)
        if err == "binary":
            skipped_binary += 1
            continue
        if err:
            errors += 1
            print(f"  [error] {f}: {err}")
            continue
        if n > 0:
            affected_files += 1
            total_matches += n
            tag = "WOULD REDACT" if not args.apply else "REDACTED    "
            print(f"  {tag} {n:>3}× {f}")

    print()
    print(f"Scanned {total_files} files "
          f"({skipped_binary} binary skipped, {errors} errors).")
    print(f"{affected_files} file(s) affected, {total_matches} total match(es).")

    if not args.apply and total_matches:
        print()
        print("Re-run with --apply to write changes (creates .scrub-bak per file).")
    if args.apply and total_matches:
        print()
        print("Next steps:")
        print("  - Rotate the secret at its source if you haven't already.")
        print("  - Clear tmux scrollback per pane:")
        print("      tmux list-windows -t <session> -F '#{window_index}' \\")
        print("        | xargs -I{} tmux clear-history -t <session>:{}.0")
        print("  - On the phone PWA: Site settings → Clear & Reset → reopen MTO.")
        print("  - Verify: re-run without --apply (should show 0 matches).")
        print("  - Backups left in place. Remove with:")
        print("      find ~/.claude ~/.cache/mobile-overlay -name '*.scrub-bak' -delete")


if __name__ == "__main__":
    main()
