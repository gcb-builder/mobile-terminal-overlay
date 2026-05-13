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
    """Heuristic: does this string look like a real password/secret?
    Real secrets typically have entropy AND character-class mixing.
    English words, identifiers, and placeholders should be rejected.
    """
    if len(value) < 8:
        return False
    # CRITICAL: never match our own redaction marker. Otherwise a
    # subsequent re-run treats [REDACTED] as a candidate and then
    # fuzzy-derives "redacted" as a base token, cascading false
    # replacements across every log that mentions the word.
    if REDACT_TOKEN in value or "redacted" in value.lower() or "[REDACT" in value.upper():
        return False
    if PLACEHOLDER_RE.match(value):
        return False
    if len(set(value)) < 5:                         # too repetitive
        return False
    # UUIDs aren't secrets
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.I):
        return False
    # Character-class mixing: secrets normally have ≥2 of the four
    # classes (lower / upper / digit / symbol). Exception: long opaque
    # strings (key tokens like sk-abc... already filtered by the
    # caller) — those bypass via class count >= 1 plus high entropy.
    cls = sum([
        bool(re.search(r"[a-z]", value)),
        bool(re.search(r"[A-Z]", value)),
        bool(re.search(r"[0-9]", value)),
        bool(re.search(r"[^A-Za-z0-9]", value)),
    ])
    entropy_proxy = len(set(value)) / len(value)
    if cls < 2 and entropy_proxy < 0.55:
        return False
    # Reject if the WHOLE value is a single English-word candidate
    # (location, credentials, settings, configuration, …). Use the
    # extended COMMON_WORDS list (defined later in file).
    if value.isalpha():
        try:
            if value.lower() in COMMON_WORDS:
                return False
        except NameError:
            pass
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


# Common English words and code identifiers we should NOT treat as a
# password "base word" when fuzzy-matching — these appear everywhere
# and would produce thousands of false positives.
COMMON_WORDS = frozenset((
    # Placeholders / config primitives
    "true false null none undefined value data type name list main "
    "test example sample placeholder password passwd secret token "
    "default user admin root host localhost server client request "
    "response config setting option string object array number "
    # Programming
    "func function class method return import export module package "
    "async await error result success status code message body header "
    "json yaml toml http https socket port path file dir folder "
    "param argument context state input output query result schema "
    "model field column index fetch update delete create insert "
    "select where order group limit offset filter map reduce filter "
    "iter loop while break continue yield catch throw raise pass "
    "lambda print logger logging debug info warn warning critical "
    "request response handler middleware route router redirect "
    # Auth / domain
    "key public private auth session login logout cookie bearer "
    "scope grant token access refresh allow deny role tenant "
    # English common (high-frequency words, 5+ chars)
    "about above across after again against along among around "
    "because before below between during except inside opposite "
    "outside through under within without again always before "
    "behind below beside cannot enough every first follow given "
    "given going great large later level might never never night "
    "often other place point right since small still their there "
    "these those today under until value where which while world "
    "would write written years young location locations setting "
    "settings credentials configuration information description "
    "operation operations execution implementation development "
    "production environment variable variables structure project "
    "projects request response message messages session sessions "
    "section sections content contents window windows process "
    "processes service services component components feature features "
    "system systems control controls option options module modules "
    "interface interfaces function functions container containers "
    "instance instances element elements property properties "
    "pattern patterns matches matching default standard generic "
    "global local remote network database table tables record records "
    "history version versions update updates change changes history "
    "primary secondary public private internal external"
).split())


def derive_base_tokens(value):
    """Extract meaningful base tokens from a password value — the
    longest non-placeholder alphabetic runs we can use as a fuzzy
    "fingerprint" to grep for variations elsewhere.

    "fearless1234!"   → {"fearless"}
    "Fearless_2024"   → {"fearless"}
    "MyToken_v2"      → {"mytoken"} (skips "v")
    "secret123"       → {} (only "secret" is alpha, in COMMON_WORDS)
    """
    runs = re.findall(r"[A-Za-z]{5,}", value)
    out = set()
    for r in runs:
        lower = r.lower()
        if lower in COMMON_WORDS:
            continue
        # Never derive "redacted" or "redact" as a base token — it'd
        # cascade-scrub every prior redaction marker on the next run.
        if lower in {"redact", "redacted"}:
            continue
        # Drop tokens with too little char variety (e.g. "aaaaaa")
        if len(set(lower)) < 4:
            continue
        out.add(lower)
    return out


def find_fuzzy_reuse(base_token, value, roots, max_per_file=5):
    """Find all occurrences of base_token (case-insensitive substring)
    across all sinks. Skips lines that are ONLY the original candidate —
    those are already covered by exact reuse. Returns Path → list of
    (line_no, line_text, matched_substring)."""
    pat = re.compile(re.escape(base_token), re.IGNORECASE)
    out = {}
    for f in find_files(roots):
        try:
            text = f.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, Exception):
            continue
        if not pat.search(text):
            continue
        hits = []
        for ln_no, ln in enumerate(text.split("\n"), start=1):
            m = pat.search(ln)
            if not m:
                continue
            # Pull the surrounding "word" containing the match — this
            # is the candidate variation (e.g. "Fearless2024_DB").
            ws = max(0, m.start() - 1)
            while ws > 0 and ln[ws - 1] not in " \t\n\r\"'`,;|<>(){}[]":
                ws -= 1
            we = m.end()
            while we < len(ln) and ln[we] not in " \t\n\r\"'`,;|<>(){}[]":
                we += 1
            variant = ln[ws:we]
            # Skip if variant IS the original candidate (exact-reuse
            # path already handles those).
            if variant == value:
                continue
            trimmed = ln if len(ln) <= 200 else ln[:197] + "…"
            hits.append((ln_no, trimmed, variant))
            if len(hits) >= max_per_file:
                break
        if hits:
            out[f] = hits
    return out


def run_reuse_scan(sinks, apply_redact=False, fuzzy=True, redact_fuzzy=False):
    """Three-phase scan, single-pass over disk:
      1. Walk all files once, extract candidate secrets from
         high-confidence contexts.
      2. Filter candidates (entropy, character classes, placeholder).
      3. Walk all files ONE MORE TIME, searching for every candidate's
         exact value AND fuzzy base token in a single read per file."""
    print("Phase 1: extracting candidate secrets from high-confidence contexts…")
    candidates = extract_candidates(sinks)
    if not candidates:
        print("  No candidates found. Nothing to cross-check.")
        return 0
    print(f"  {len(candidates)} unique candidate(s) found (post-filter).")
    if len(candidates) > 100:
        print(f"  WARNING: {len(candidates)} is high — extractors may be too loose.")

    # Build the lookup tables once.
    exact_pats = {v: re.compile(re.escape(v)) for v in candidates}
    fuzzy_tokens = {}  # base_token → originating candidate value
    if fuzzy:
        for value in candidates:
            for tok in derive_base_tokens(value):
                # Last write wins on collision — fine, we only need
                # *some* originating value for display.
                fuzzy_tokens[tok] = value
    fuzzy_pats = {tok: re.compile(re.escape(tok), re.IGNORECASE) for tok in fuzzy_tokens}

    # Aggregators
    exact_hits = {v: {} for v in candidates}     # value → {path → [(ln, ln_text)]}
    fuzzy_hits = {tok: {} for tok in fuzzy_tokens}  # token → {path → [(ln, ln_text, variant)]}

    print()
    print(f"Phase 2: scanning sinks (single-pass over each file)…")
    n_files = 0
    for f in find_files(sinks):
        n_files += 1
        try:
            text = f.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, Exception):
            continue
        # Exact value matches (line-level for context)
        lines = None
        for value, pat in exact_pats.items():
            if value not in text:
                continue
            if lines is None:
                lines = text.split("\n")
            hits = []
            for ln_no, ln in enumerate(lines, start=1):
                if pat.search(ln):
                    trimmed = ln if len(ln) <= 200 else ln[:197] + "…"
                    hits.append((ln_no, trimmed))
                    if len(hits) >= 5:
                        break
            if hits:
                exact_hits[value][f] = hits
        # Fuzzy token matches
        for tok, pat in fuzzy_pats.items():
            if not pat.search(text):
                continue
            if lines is None:
                lines = text.split("\n")
            hits = []
            for ln_no, ln in enumerate(lines, start=1):
                m = pat.search(ln)
                if not m:
                    continue
                ws = max(0, m.start() - 1)
                while ws > 0 and ln[ws - 1] not in " \t\n\r\"'`,;|<>(){}[]":
                    ws -= 1
                we = m.end()
                while we < len(ln) and ln[we] not in " \t\n\r\"'`,;|<>(){}[]":
                    we += 1
                variant = ln[ws:we]
                if variant == fuzzy_tokens[tok]:
                    continue   # exact-match path covers it
                trimmed = ln if len(ln) <= 200 else ln[:197] + "…"
                hits.append((ln_no, trimmed, variant))
                if len(hits) >= 5:
                    break
            if hits:
                fuzzy_hits[tok][f] = hits
    print(f"  scanned {n_files} files.")
    print()

    # Report exact reuse
    print("Phase 3a: EXACT reuse")
    print()
    total_redacted = 0
    any_exact = False
    for value, sources in sorted(candidates.items(), key=lambda kv: -len(kv[1])):
        reuse = exact_hits[value]
        if not reuse:
            continue
        any_exact = True
        total = sum(len(v) for v in reuse.values())
        print(f"━ candidate: {_mask(value)}  (first seen via {sources[0][1]})")
        print(f"  exact: {total} occurrence(s) across {len(reuse)} file(s):")
        for path, hits in list(reuse.items())[:6]:
            print(f"    {path}")
            for ln_no, ln in hits[:2]:
                print(f"      L{ln_no}: {ln[:140]}")
        if len(reuse) > 6:
            print(f"    … and {len(reuse) - 6} more file(s)")
        print()
        if apply_redact:
            for path in reuse:
                k, _ = scrub_file(path, exact_pats[value], True)
                total_redacted += k
    if not any_exact:
        print("  (none)")
        print()

    # Report fuzzy reuse
    if fuzzy:
        print("Phase 3b: FUZZY reuse — variations sharing a base token")
        print()
        any_fuzzy = False
        for tok, hits_by_file in sorted(fuzzy_hits.items(),
                                         key=lambda kv: -sum(len(v) for v in kv[1].values())):
            if not hits_by_file:
                continue
            any_fuzzy = True
            distinct_variants = set()
            total = 0
            for hits in hits_by_file.values():
                for _, _, var in hits:
                    distinct_variants.add(var)
                    total += 1
            if not distinct_variants:
                continue
            print(f"━ base token: {_mask(tok, keep=6)}  (from candidate {_mask(fuzzy_tokens[tok])})")
            print(f"  fuzzy: {total} hit(s), {len(hits_by_file)} file(s), "
                  f"{len(distinct_variants)} distinct variant(s):")
            for var in list(distinct_variants)[:6]:
                print(f"    variant: {_mask(var, keep=6)}")
            for path, hits in list(hits_by_file.items())[:4]:
                print(f"    {path}")
                for ln_no, ln, var in hits[:2]:
                    print(f"      L{ln_no} ({_mask(var, keep=6)}): {ln[:120]}")
            if len(hits_by_file) > 4:
                print(f"    … and {len(hits_by_file) - 4} more file(s)")
            print()
        if not any_fuzzy:
            print("  (none)")
            print()

    fuzzy_hit_count = (sum(sum(len(v) for v in fh.values()) for fh in fuzzy_hits.values())
                       if fuzzy else 0)

    # Optional: redact every distinct fuzzy variant as a literal. Logs
    # are local-only and we have backups, so a few false-positive
    # redactions (project names sharing the base token, etc.) are
    # acceptable losses against the upside of one-shot cleanup.
    fuzzy_redacted = 0
    if apply_redact and redact_fuzzy and fuzzy:
        all_variants = set()
        for hits_by_file in fuzzy_hits.values():
            for hits in hits_by_file.values():
                for _, _, var in hits:
                    if len(var) >= 6:
                        all_variants.add(var)
        # Build one combined regex of all variants for a single pass
        # per file (avoids O(V × N) re-reads).
        if all_variants:
            print()
            print(f"Phase 4: redacting {len(all_variants)} distinct fuzzy variants…")
            big_pat = re.compile("|".join(re.escape(v) for v in all_variants))
            for f in find_files(sinks):
                try:
                    text = f.read_bytes().decode("utf-8")
                except (UnicodeDecodeError, Exception):
                    continue
                if not big_pat.search(text):
                    continue
                bak = f.with_suffix(f.suffix + ".scrub-bak")
                if not bak.exists():
                    try:
                        bak.write_bytes(text.encode("utf-8"))
                    except Exception as e:
                        print(f"    [error] backup failed for {f}: {e}")
                        continue
                new_text, n = big_pat.subn(REDACT_TOKEN, text)
                if n:
                    try:
                        f.write_text(new_text, encoding="utf-8")
                        fuzzy_redacted += n
                    except Exception as e:
                        print(f"    [error] write failed for {f}: {e}")
            print(f"  fuzzy-variant redactions: {fuzzy_redacted}")

    print()
    print(f"Summary: exact redacted={total_redacted}, "
          f"fuzzy redacted={fuzzy_redacted}, "
          f"fuzzy hits surfaced={fuzzy_hit_count}")
    if not apply_redact:
        print("Re-run with --apply to redact every EXACT match.")
        print("Add --include-fuzzy to ALSO redact every fuzzy variant in one shot")
        print("(may zap project names sharing the base; backups left as .scrub-bak).")
    elif apply_redact and not redact_fuzzy and fuzzy and fuzzy_hit_count:
        print("Add --include-fuzzy to redact the fuzzy variants too.")
    return


# (the older two-phase run_reuse_scan was replaced by the
# three-phase fuzzy version above)


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
    ap.add_argument("--include-fuzzy", action="store_true",
                    help="With --reuse-scan --apply, also redact every "
                         "distinct fuzzy variant as a literal. Logs are "
                         "local-only and backups exist, so the cost of a "
                         "false positive is just losing some log "
                         "readability. Variants shorter than 6 chars "
                         "are skipped as a safety floor.")
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
        print(f"Mode  : REUSE-SCAN  (apply={args.apply}, include-fuzzy={args.include_fuzzy})")
        print(f"Sinks :")
        for s in sinks:
            resolved = Path(os.path.expanduser(s))
            marker = "✓" if resolved.exists() else "—"
            print(f"  {marker} {s}")
        print()
        run_reuse_scan(sinks, apply_redact=args.apply, redact_fuzzy=args.include_fuzzy)
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
