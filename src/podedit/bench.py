"""Benchmark harness: wall time, peak RSS, success/error.

Records every run as a JSON line to ``benchmarks.jsonl`` so we can compare
models/devices on real episodes over time. ``process_peak_rss_mb`` is the
*process-cumulative* maximum (Linux ``ru_maxrss`` in KB / Darwin in bytes),
not interval-specific — sufficient for ASR runs where each run is its own
process, but be aware when chaining multiple measures in one process.
"""
from __future__ import annotations

import json
import os
import resource
import time
from contextlib import contextmanager
from pathlib import Path


def _process_peak_rss_mb() -> float:
    val = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if os.uname().sysname == "Darwin":  # bytes
        return val / (1024.0 * 1024.0)
    return val / 1024.0  # Linux: KB


@contextmanager
def measure(label: str, log_path: Path | None = None, extra: dict | None = None):
    """Time a code block, capture peak RSS, and append a JSON record on exit.

    The yielded dict can be mutated to add fields (e.g. resolved device, output sizes).
    On exception, the record is still written with ``success: false`` and exception info,
    then the exception is re-raised.
    """
    rec: dict = {
        "label": label,
        "started_at": time.time(),
        "extra": dict(extra) if extra else {},
        "success": True,
    }
    start = time.perf_counter()
    try:
        yield rec
    except BaseException as exc:
        rec["success"] = False
        rec["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        rec["wall_sec"] = time.perf_counter() - start
        rec["process_peak_rss_mb"] = _process_peak_rss_mb()
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
