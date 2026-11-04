"""Worker resource measurements for job results."""

import threading
from pathlib import Path

CGROUP_MEMORY_FILES = (
    Path("/sys/fs/cgroup/memory.current"),
    Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
)
SAMPLE_INTERVAL_SECONDS = 0.1


class PeakMemorySampler:
    """Sample the worker container's cgroup memory usage during a task."""

    def __init__(self) -> None:
        self.peak_bytes: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "PeakMemorySampler":
        self._sample()
        if self.peak_bytes is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._sample()

    def _sample(self) -> None:
        for path in CGROUP_MEMORY_FILES:
            try:
                usage = int(path.read_text(encoding="ascii").strip())
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                return
            self.peak_bytes = max(self.peak_bytes or 0, usage)
            return

    def _run(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL_SECONDS):
            self._sample()
