import logging
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

REDRAW_INTERVAL_S = 0.05


class ProgressLine:

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.enabled = stream.isatty()
        self.text = ""
        self.last_draw = 0.0

    def show(self, text: str) -> None:
        now = time.monotonic()
        if not self.enabled or now - self.last_draw < REDRAW_INTERVAL_S:
            return
        self.last_draw = now
        self.text = text[: shutil.get_terminal_size().columns - 1]
        self._draw()

    def print_above(self, message: str) -> None:
        if self.enabled:
            self.stream.write("\r\033[K")
        self.stream.write(message + "\n")
        self._draw()

    def clear(self) -> None:
        if self.enabled and self.text:
            self.stream.write("\r\033[K")
            self.stream.flush()
        self.text = ""

    def _draw(self) -> None:
        if self.enabled:
            self.stream.write("\r\033[K" + self.text)
        self.stream.flush()


class _PrintAbove(logging.Handler):
    def __init__(self, line: ProgressLine, shorten_prefix: str) -> None:
        super().__init__()
        self.line = line
        self.shorten_prefix = shorten_prefix

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        if self.shorten_prefix:
            message = message.replace(self.shorten_prefix, "")
        self.line.print_above(message)


@contextmanager
def progress_line(shorten_prefix: str = "") -> Iterator[ProgressLine]:

    line = ProgressLine(sys.stderr)
    handler = _PrintAbove(line, shorten_prefix)
    handler.setFormatter(logging.Formatter("  ! %(message)s"))
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = [handler]
    try:
        yield line
    finally:
        line.clear()
        root.handlers = saved


def bar(done: int, total: int, width: int = 24) -> str:
    fraction = done / total if total else 1.0
    filled = round(width * fraction)
    return f"{'█' * filled}{'░' * (width - filled)} {done:,}/{total:,} {fraction:4.0%}"
