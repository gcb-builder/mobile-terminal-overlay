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
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes. Default is dry-run.")
    ap.add_argument("--include-uploads", action="store_true",
                    help="Also scrub .claude/uploads/ in --repo (text files only).")
    ap.add_argument("--repo", help="Repo path for --include-uploads (default: cwd).")
    ap.add_argument("--sink", action="append",
                    help="Extra sink path to scan (repeatable).")
    args = ap.parse_args()

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

    sinks = list(DEFAULT_SINKS)
    if args.include_uploads:
        repo = Path(args.repo or os.getcwd()).expanduser().resolve()
        sinks.append(str(repo / ".claude" / "uploads"))
    if args.sink:
        sinks.extend(args.sink)

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
