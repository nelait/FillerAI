"""The running commentary a training run gives while it works.

Training a model over a wide form takes a few seconds, and a few seconds of
a blank screen is indistinguishable from a hang. So every stage says what it
is doing as it does it, and whoever asked can watch.

There is one class here and it is deliberately dull: an append-only list of
lines with a lock around it. The reason for the lock is that the web UI runs
the fit on a worker thread and polls for the lines from the request threads,
so appends and reads genuinely do cross threads. The reason for the cursor
is the same: the UI asks "what is new since line 40?" every few hundred
milliseconds, and the answer has to be exact even while lines are arriving.

A trace is optional everywhere. :func:`train` takes one or does not, and the
code that emits lines never checks - :data:`SILENT` swallows them, so there
is no ``if trace is not None`` scattered through the fitting code.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

# Levels, in the order a reader cares about them. "step" is the one that
# matters: it marks the start of a stage, and a UI can show only those and
# still tell the story.
LEVELS = ("step", "info", "detail", "warn", "done")


@dataclass
class Line:
    """One logged moment."""

    index: int
    at: float  # seconds since the trace started
    level: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "at": round(self.at, 3),
                "level": self.level, "text": self.text}

    def formatted(self) -> str:
        marker = {"step": "==", "warn": "!!", "done": "ok"}.get(self.level, "  ")
        return f"[{self.at:6.2f}s] {marker} {self.text}"


class Trace:
    """Lines emitted by a run, readable while the run is still going."""

    def __init__(self, echo: bool = False) -> None:
        self._lines: list[Line] = []
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._step = 0
        self._steps = 0
        self._finished = False
        self._failure: str | None = None
        # Somewhere for a caller to hang the things a UI wants alongside the
        # log: the script, the run's parameters, its result.
        self.extra: dict[str, Any] = {}
        self.echo = echo

    # -- writing -----------------------------------------------------------

    def log(self, text: str, level: str = "info") -> None:
        with self._lock:
            line = Line(index=len(self._lines), at=time.monotonic() - self._started,
                        level=level, text=text)
            self._lines.append(line)
        if self.echo:
            print(line.formatted(), flush=True)

    def step(self, text: str) -> None:
        """Start a stage. Counted, so progress can be shown as a fraction."""
        with self._lock:
            self._step += 1
            self._steps = max(self._steps, self._step)
        self.log(text, "step")

    def detail(self, text: str) -> None:
        self.log(text, "detail")

    def warn(self, text: str) -> None:
        self.log(text, "warn")

    def done(self, text: str) -> None:
        self.log(text, "done")

    def expect(self, steps: int) -> None:
        """Say up front how many stages there will be, for a progress bar."""
        with self._lock:
            self._steps = max(steps, self._step)

    def finish(self, failure: str | None = None) -> None:
        with self._lock:
            self._finished = True
            self._failure = failure
        if failure:
            self.log(failure, "warn")

    # -- reading -----------------------------------------------------------

    def since(self, cursor: int = 0) -> list[Line]:
        """Every line from ``cursor`` on. Safe to call while lines arrive."""
        with self._lock:
            if cursor <= 0:
                return list(self._lines)
            return self._lines[cursor:]

    def text(self) -> str:
        return "\n".join(line.formatted() for line in self.since())

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._lines)

    @property
    def finished(self) -> bool:
        with self._lock:
            return self._finished

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def progress(self) -> dict[str, Any]:
        with self._lock:
            step, steps = self._step, self._steps
            finished, failure = self._finished, self._failure
        return {
            "step": step,
            "steps": steps,
            "fraction": round(step / steps, 3) if steps else 0.0,
            "elapsed": round(self.elapsed, 2),
            "finished": finished,
            "failure": failure,
        }


class _Silent(Trace):
    """A trace that keeps nothing, for the common case of nobody watching.

    Overriding the writes rather than guarding every call site: a fit over a
    wide form emits a few thousand lines, and holding them for a caller who
    never asked is the sort of cost that only shows up on the big form.
    """

    def __init__(self) -> None:
        super().__init__(echo=False)

    def log(self, text: str, level: str = "info") -> None:
        return

    def step(self, text: str) -> None:
        with self._lock:
            self._step += 1
            self._steps = max(self._steps, self._step)


SILENT = _Silent()


def resolve(trace: Trace | None) -> Trace:
    """Whatever the caller passed, or a trace that discards."""
    return SILENT if trace is None else trace


def count_words(items: Iterable[Any], singular: str, plural: str | None = None) -> str:
    """``3 fields`` / ``1 field`` - small, and used in a lot of log lines."""
    total = len(list(items))
    word = singular if total == 1 else (plural or singular + "s")
    return f"{total} {word}"
