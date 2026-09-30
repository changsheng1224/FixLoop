"""Process generation identity; unknown access never proves termination."""

from __future__ import annotations

import os
from pathlib import Path


def process_identity(pid: int) -> dict | None:
    if os.name != "nt":
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
            fields = text[text.rfind(")") + 2 :].split()
            if fields[0] == "Z":
                return None
            return {
                "pid": pid,
                "generation": fields[19],
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            }
        except FileNotFoundError:
            return None
        except (OSError, IndexError):
            return {"pid": pid, "generation": "unknown"}
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None if ctypes.get_last_error() == 87 else {"pid": pid, "generation": "unknown"}
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return {"pid": pid, "generation": "unknown"}
        return {"pid": pid, "generation": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}
    finally:
        kernel.CloseHandle(handle)


def confirmed_exited(identity: dict) -> bool:
    if not identity or identity.get("generation") in {None, "unknown"}:
        return False
    current = process_identity(identity["pid"])
    return current is None or (current.get("generation") != "unknown" and current != identity)
