"""The run's own soft deadline, checked between pages (as ocr-service does), never Celery's soft limit.

``recode_pdf`` assembles a whole book in one process, so a page boundary is not visible from
here while it runs: recode_pdf and veraPDF get only the time that is left (``remaining()``) as
their timeout instead. Between pages, profiles and leaves the job calls ``check()``.

Not bounded: the reflow edition's pandoc runs (one per formula check, one for the EPUB), a
verbatim port without a timeout. A pandoc that hangs is stopped by Celery's hard limit, without
an outcome file; the orchestrator then ends the job as lost.
"""

from __future__ import annotations

import time


class DeadlineExceeded(RuntimeError):
    """The book passed its soft deadline; the job writes a ``failed`` outcome before Celery's hard limit."""


class Deadline:
    def __init__(self, seconds: float, *, clock=time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._at = clock() + seconds

    def remaining(self) -> float:
        """Seconds left, at least 1 (a subprocess timeout of 0 would mean "no limit" to some callers)."""
        return max(self._at - self._clock(), 1.0)

    def expired(self) -> bool:
        return self._clock() > self._at

    def check(self) -> None:
        if self.expired():
            raise DeadlineExceeded(f"time limit exceeded ({self.seconds:.0f} s); the book was abandoned")
