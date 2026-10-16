"""Bounded operation counters and timings without queries, text or credentials."""

import math
import shutil
import threading
from collections import Counter, defaultdict, deque


class Telemetry:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts = Counter()
        self.timings = defaultdict(lambda: deque(maxlen=2048))

    def observe(self, operation, status, milliseconds):
        with self.lock:
            self.counts[(operation, status)] += 1
            self.timings[operation].append(milliseconds)

    def snapshot(self):
        with self.lock:
            result = {}
            for operation, values in self.timings.items():
                ordered = sorted(values)
                result[operation] = {"sample_count": len(ordered), **{
                    "p{}{}_ms".format(int(fraction * 100), ""): round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)
                    for fraction in (.5, .95, .99)}}
            return {"counts": [{"operation": key[0], "status": key[1], "count": count}
                               for key, count in self.counts.items()], "rolling_timings": result}


def resources(root):
    """Report OS process RSS where available; do not invent unavailable figures."""
    import os
    rss = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Memory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("peak_working", "working", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
        memory = Memory()
        memory.cb = ctypes.sizeof(memory)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(memory), memory.cb):
            rss = memory.working
    else:
        try:
            # Linux current RSS, not cumulative ru_maxrss. No source content.
            from pathlib import Path
            rss = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError, AttributeError):
            pass
    return {"rss_bytes": rss, "threads": threading.active_count(), "free_disk_bytes": shutil.disk_usage(root).free}
