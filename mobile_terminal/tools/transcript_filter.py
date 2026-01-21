#!/usr/bin/env python3
"""
Transcript filter for tmux pipe-pane output.

Filters out:
- ANSI escape codes (colors, cursor movement)
- OSC sequences (terminal titles, etc.)
- Spinner animation frames
- Carriage-return redraws (keeps last version)
- Empty/whitespace-only lines

Usage:
    tmux pipe-pane -o -t TARGET "python3 -u /path/to/transcript_filter.py >> transcript.log"

The -u flag ensures unbuffered output for real-time logging.
"""
import sys
import re

# ANSI CSI sequences: ESC [ ... final_byte
ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# OSC sequences: ESC ] ... (BEL or ST)
ANSI_OSC = re.compile(r"\x1b\][^\x07]*(\x07|\x1b\\)")

# Other escape sequences (simple ones)
ANSI_SIMPLE = re.compile(r"\x1b[()][AB012]|\x1b[=>]|\x1b[78]")

# Spinner glyphs used by Claude and common TUIs
SPINNER_CHARS = frozenset("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏|/-\\◐◓◑◒●○◉◎")


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    text = ANSI_OSC.sub("", text)
    text = ANSI_CSI.sub("", text)
    text = ANSI_SIMPLE.sub("", text)
    return text


def is_spinner_frame(raw: str, clean: str) -> bool:
    """
    Detect if a line is just a spinner animation frame.
    These are high-frequency visual noise.
    """
    # Lots of escapes but very few printable chars = likely spinner
    if "\x1b" in raw:
        printable = re.sub(r"[\x00-\x1f\x7f]", "", clean)
        if len(printable) < 4:
            return True

    # Contains spinner glyph + carriage return = definitely spinner
    if "\r" in raw and any(c in raw for c in SPINNER_CHARS):
        return True

    # Line is just a spinner character
    if clean.strip() in SPINNER_CHARS:
        return True

    return False


def is_cursor_noise(raw: str) -> bool:
    """Detect cursor visibility toggles and position saves (noise)."""
    # ?25l = hide cursor, ?25h = show cursor
    if re.search(r"\x1b\[\?25[lh]", raw):
        # If that's basically all the line is, skip it
        clean = strip_ansi(raw).strip()
        if len(clean) < 2:
            return True
    return False


def process_line(raw: str) -> str | None:
    """
    Process a single line of input.
    Returns cleaned line or None if it should be dropped.
    """
    # Handle carriage-return redraws: keep content after last CR
    # This collapses "progress bar" style updates into final state
    if "\r" in raw and "\n" not in raw:
        parts = raw.split("\r")
        raw = parts[-1]  # Keep last segment

    # Strip ANSI codes
    clean = strip_ansi(raw)

    # Drop empty lines
    if not clean.strip():
        return None

    # Drop spinner frames
    if is_spinner_frame(raw, clean):
        return None

    # Drop cursor noise
    if is_cursor_noise(raw):
        return None

    # Convert any remaining CRs to newlines
    clean = clean.replace("\r", "\n")

    return clean


def main():
    """Main filter loop - reads stdin, writes filtered output to stdout."""
    try:
        for raw in sys.stdin:
            result = process_line(raw)
            if result:
                sys.stdout.write(result)
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
