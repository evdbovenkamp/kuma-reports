"""Give freed heap memory back to the OS after heavy work (glibc malloc_trim).

CPython keeps freed memory in its heap; on a small VPS next to Uptime Kuma that matters.
"""
import ctypes

try:
    _trim = ctypes.CDLL("libc.so.6").malloc_trim
except (OSError, AttributeError):  # non-glibc platforms
    _trim = None


def release() -> None:
    if _trim:
        _trim(0)
