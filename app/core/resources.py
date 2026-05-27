from __future__ import annotations

import os
import resource
import threading
import time


def resource_snapshot() -> dict[str, int | float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss_kb = int(usage.ru_maxrss)
    return {
        "pid": os.getpid(),
        "rss_kb": rss_kb,
        "thread_count": threading.active_count(),
        "process_cpu_time_ms": round(time.process_time() * 1000, 2),
    }
