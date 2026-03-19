"""ProcessRuntime Protocol and TmuxRuntime implementation.

Consolidates all PTY lifecycle, I/O, and tmux command interactions
behind a single interface for testability and consistency.

tmux target format:
    session             — e.g. "main"
    session:window      — e.g. "main:0"
    session:window.pane — e.g. "main:0.1"
"""

import fcntl
import logging
import os
import pty
import signal
import struct
import termios
import time
from typing import Optional, Protocol, runtime_checkable

from mobile_terminal.helpers import run_subprocess

logger = logging.getLogger(__name__)


@runtime_checkable
class ProcessRuntime(Protocol):
    """Interface for process interaction (PTY + tmux)."""

    # -- State -----------------------------------------------------------------

    @property
    def master_fd(self) -> Optional[int]: ...

    @property
    def child_pid(self) -> Optional[int]: ...

    @property
    def session_name(self) -> Optional[str]:
        """Last session spawned (for debugging). May be None before spawn()."""
        ...

    @property
    def has_fd(self) -> bool:
        """True when the PTY file descriptor is open."""
        ...

    # -- PTY lifecycle ---------------------------------------------------------

    def spawn(self, session_name: str) -> tuple[int, int]:
        """Fork + exec tmux, returning (master_fd, child_pid)."""
        ...

    def terminate(self, force: bool = False) -> str:
        """Send SIGTERM (and optionally SIGKILL). Returns method used."""
        ...

    def close_fd(self) -> None:
        """Close the master fd and clear PTY state."""
        ...

    # -- Direct PTY I/O (real-time streams) ------------------------------------

    def pty_write(self, data: bytes) -> None:
        """Write raw bytes to the PTY. Never modifies data.

        Raises RuntimeError if no fd is open.
        """
        ...

    def pty_read(self, bufsize: int = 4096) -> bytes:
        """Read raw bytes from the PTY.

        Raises RuntimeError if no fd is open.
        """
        ...

    def write_command(self, command: str) -> None:
        r"""Write command + CR to the PTY. Always appends ``\r``.

        Raises RuntimeError if no fd is open.
        """
        ...

    # -- tmux send-keys (atomic text, avoids interleaving) ---------------------

    async def send_keys(
        self, target: str, *keys: str, literal: bool = False
    ) -> None: ...

    # -- Window / pane management ----------------------------------------------

    async def new_window(
        self, session: str, name: str, cwd: str = ""
    ) -> str: ...

    async def kill_window(self, target: str) -> None: ...

    async def select_window(self, target: str) -> None: ...

    async def select_pane(self, target: str) -> None: ...

    # -- tmux queries ----------------------------------------------------------

    async def capture_pane(
        self, target: str, lines: int = 50, ansi: bool = False
    ) -> str: ...

    async def display_message(self, target: str, fmt: str) -> str: ...

    async def list_panes(self, session: str, fmt: str = "") -> str: ...

    async def pipe_pane(self, target: str, command: str = "") -> None: ...

    # -- Terminal size ---------------------------------------------------------

    def set_size(self, cols: int, rows: int) -> None: ...


# ==========================================================================
# Concrete implementation
# ==========================================================================


class TmuxRuntime:
    """Concrete ProcessRuntime backed by a PTY + tmux."""

    def __init__(self) -> None:
        self._master_fd: Optional[int] = None
        self._child_pid: Optional[int] = None
        self._session_name: Optional[str] = None

    # -- State -----------------------------------------------------------------

    @property
    def master_fd(self) -> Optional[int]:
        return self._master_fd

    @property
    def child_pid(self) -> Optional[int]:
        return self._child_pid

    @property
    def session_name(self) -> Optional[str]:
        return self._session_name

    @property
    def has_fd(self) -> bool:
        return self._master_fd is not None

    # -- PTY lifecycle ---------------------------------------------------------

    def spawn(self, session_name: str) -> tuple[int, int]:
        """Spawn ``tmux new -A -s <session>`` via subprocess.Popen.

        Uses Popen instead of os.fork() to avoid segfaults caused by
        forking inside an asyncio process with active threads/locks.

        Returns (master_fd, child_pid).
        """
        import subprocess

        master_fd, slave_fd = pty.openpty()

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"

        try:
            proc = subprocess.Popen(
                ["tmux", "new", "-A", "-s", session_name],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)

        self._master_fd = master_fd
        self._child_pid = proc.pid
        self._session_name = session_name
        return master_fd, proc.pid

    def terminate(self, force: bool = False) -> str:
        """Send SIGTERM; optionally SIGKILL after 0.5 s.

        Returns the method used: ``"SIGTERM"``, ``"SIGKILL"``, or
        ``"already_dead"``.
        """
        pid = self._child_pid
        if pid is None:
            return "already_dead"

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "already_dead"

        # Wait briefly for graceful shutdown
        time.sleep(0.5)

        try:
            os.kill(pid, 0)
            still_running = True
        except OSError:
            still_running = False

        if still_running and force:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            time.sleep(0.2)
            return "SIGKILL"

        return "SIGTERM"

    def close_fd(self) -> None:
        """Close the master PTY fd and clear state."""
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except Exception:
                pass
        self._master_fd = None
        self._child_pid = None

    # -- Direct PTY I/O --------------------------------------------------------

    def pty_write(self, data: bytes) -> None:
        """Write raw bytes to PTY. Never modifies *data*."""
        if self._master_fd is None:
            raise RuntimeError("No PTY fd — call spawn() first")
        os.write(self._master_fd, data)

    def pty_read(self, bufsize: int = 4096) -> bytes:
        """Read raw bytes from PTY."""
        if self._master_fd is None:
            raise RuntimeError("No PTY fd — call spawn() first")
        return os.read(self._master_fd, bufsize)

    def write_command(self, command: str) -> None:
        r"""Write *command* + ``\r`` to PTY.

        This is the **only** method that appends a carriage return.
        ``pty_write()`` never modifies the bytes it receives.
        """
        self.pty_write((command + "\r").encode("utf-8"))

    # -- tmux send-keys --------------------------------------------------------

    async def send_keys(
        self, target: str, *keys: str, literal: bool = False
    ) -> None:
        """Send keys to a tmux pane via ``tmux send-keys``."""
        cmd = ["tmux", "send-keys", "-t", target]
        if literal:
            cmd.append("-l")
        cmd.extend(keys)
        await run_subprocess(cmd, timeout=5)

    # -- Window / pane management ----------------------------------------------

    async def new_window(
        self, session: str, name: str, cwd: str = ""
    ) -> str:
        """Create a new tmux window. Returns window target string."""
        cmd = ["tmux", "new-window", "-a", "-t", session, "-n", name]
        if cwd:
            cmd.extend(["-c", cwd])
        result = await run_subprocess(cmd, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(
                f"tmux new-window failed: {result.stderr.strip()}"
            )
        return f"{session}:{name}"

    async def kill_window(self, target: str) -> None:
        await run_subprocess(
            ["tmux", "kill-window", "-t", target], timeout=5
        )

    async def select_window(self, target: str) -> None:
        await run_subprocess(
            ["tmux", "select-window", "-t", target],
            capture_output=True, timeout=2,
        )

    async def select_pane(self, target: str) -> None:
        await run_subprocess(
            ["tmux", "select-pane", "-t", target],
            capture_output=True, timeout=2,
        )

    # -- tmux queries ----------------------------------------------------------

    async def capture_pane(
        self, target: str, lines: int = 50, ansi: bool = False
    ) -> str:
        """Capture pane content via ``tmux capture-pane -p``."""
        cmd = ["tmux", "capture-pane", "-p", "-S", f"-{lines}", "-t", target]
        if ansi:
            cmd.insert(3, "-e")  # insert before -S
        result = await run_subprocess(cmd, capture_output=True, text=True, timeout=5)
        return result.stdout if result.returncode == 0 else ""

    async def display_message(self, target: str, fmt: str) -> str:
        """Run ``tmux display-message`` and return stdout."""
        result = await run_subprocess(
            ["tmux", "display-message", "-t", target, "-p", fmt],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    async def list_panes(self, session: str, fmt: str = "") -> str:
        """Run ``tmux list-panes -s`` and return stdout."""
        cmd = ["tmux", "list-panes", "-s", "-t", session]
        if fmt:
            cmd.extend(["-F", fmt])
        result = await run_subprocess(
            cmd, capture_output=True, text=True, timeout=5
        )
        return result.stdout if result.returncode == 0 else ""

    async def pipe_pane(self, target: str, command: str = "") -> None:
        """Start or stop ``tmux pipe-pane``.

        Pass a non-empty *command* to start piping, or empty string to stop.
        """
        cmd = ["tmux", "pipe-pane", "-t", target]
        if command:
            cmd.append(command)
        await run_subprocess(cmd, timeout=3)

    # -- Terminal size ---------------------------------------------------------

    def set_size(self, cols: int, rows: int) -> None:
        """Set PTY size and send SIGWINCH to child."""
        if self._master_fd is None:
            raise RuntimeError("No PTY fd — call spawn() first")
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        if self._child_pid:
            try:
                os.kill(self._child_pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass
