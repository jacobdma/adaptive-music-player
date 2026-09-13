import os
import select
import shutil
import sys
import termios
import tty
from collections.abc import Iterator
from contextlib import contextmanager

KEYS = {
    " ": "pause", "n": "next", "p": "previous", "l": "like", "q": "quit",
    "\x1b[C": "forward", "\x1b[D": "back",
}
HELP = "space pause · n next · p previous · l like · ←/→ seek 10s · q quit"


@contextmanager
def raw_terminal() -> Iterator[None]:
    """Read single keypresses without echo."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\033[?25l")  # hide cursor
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()


def read_keys(timeout_s: float) -> list[str]:
    """Return the actions for keys pressed within the timeout."""
    ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    if not ready:
        return []
    data = os.read(sys.stdin.fileno(), 64).decode(errors="ignore")
    actions = []
    while data:
        size = 3 if data.startswith("\x1b[") else 1
        key, data = data[:size], data[size:]
        if (action := KEYS.get(key.lower())) is not None:
            actions.append(action)
    return actions


def show_status(text: str) -> None:
    width = shutil.get_terminal_size().columns
    sys.stdout.write("\r\033[K" + text[: width - 1])
    sys.stdout.flush()


def print_line(text: str) -> None:
    sys.stdout.write("\r\033[K" + text + "\n")
    sys.stdout.flush()


def format_ms(ms: int) -> str:
    return f"{ms // 60000}:{ms // 1000 % 60:02d}"
