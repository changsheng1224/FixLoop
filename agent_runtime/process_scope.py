"""Windows process-tree ownership shared by execution services."""

import time
import uuid

from agent_runtime.plan_runtime.processes import confirmed_exited, process_identity


class CleanupUnconfirmedError(RuntimeError):
    pass


class WindowsJob:
    """Non-inherited kill-on-close Job; children cannot request breakaway."""

    backend = "windows_job"

    def __init__(self, name=None, *, existing=False):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.name = name or "Local\\FixLoop-" + uuid.uuid4().hex
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for method, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("OpenJobObjectW", [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            (
                "SetInformationJobObject",
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            (
                "QueryInformationJobObject",
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self.api, method)
            function.argtypes, function.restype = args, result
        self.handle = (
            self.api.OpenJobObjectW(0x4 | 0x8, False, self.name)
            if existing
            else self.api.CreateJobObjectW(None, self.name)
        )
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not existing:
            # Fresh random names are never allowed to adopt an existing Job.
            if ctypes.get_last_error() == 183:
                self.close()
                raise CleanupUnconfirmedError("job identity collision")

            class Basic(ctypes.Structure):
                _fields_ = [
                    ("user", ctypes.c_longlong),
                    ("job_user", ctypes.c_longlong),
                    ("flags", wintypes.DWORD),
                    ("minimum", ctypes.c_size_t),
                    ("maximum", ctypes.c_size_t),
                    ("active_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD),
                ]

            class Extended(ctypes.Structure):
                _fields_ = [
                    ("basic", Basic),
                    ("io", ctypes.c_ulonglong * 6),
                    ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t),
                    ("peak_job", ctypes.c_size_t),
                ]

            limits = Extended()
            limits.basic.flags = 0x2000 | 0x8  # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS
            limits.basic.active_limit = 128
            try:
                self._check(
                    self.api.SetInformationJobObject(
                        self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                    )
                )
            except BaseException:
                self.close()
                raise

    def _check(self, result):
        if not result:
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def attach(self, pid):
        process = self.api.OpenProcess(0x100 | 0x1, False, pid)
        self._check(process)
        try:
            self._check(self.api.AssignProcessToJobObject(self.handle, process))
        finally:
            self.api.CloseHandle(process)

    def empty(self):
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION has four LARGE_INTEGERs and
        # four DWORDs. ActiveProcesses is DWORD index 2 following the times.
        data = (self.ctypes.c_ulonglong * 6)()
        self._check(
            self.api.QueryInformationJobObject(
                self.handle, 1, self.ctypes.byref(data), self.ctypes.sizeof(data), None
            )
        )
        integers = self.ctypes.cast(
            self.ctypes.byref(data, 32), self.ctypes.POINTER(self.ctypes.c_uint32)
        )
        return integers[2] == 0

    def terminate(self, timeout=3):
        buffer = self.ctypes.create_string_buffer(
            8 + 128 * self.ctypes.sizeof(self.ctypes.c_size_t)
        )
        self._check(self.api.QueryInformationJobObject(self.handle, 3, buffer, len(buffer), None))
        count = self.ctypes.cast(buffer, self.ctypes.POINTER(self.ctypes.c_uint32))[1]
        pids = self.ctypes.cast(
            self.ctypes.byref(buffer, 8), self.ctypes.POINTER(self.ctypes.c_size_t)
        )
        identities = [identity for i in range(count) if (identity := process_identity(pids[i]))]
        self._check(self.api.TerminateJobObject(self.handle, 1))
        deadline = time.monotonic() + timeout
        while not self.empty() or any(not confirmed_exited(identity) for identity in identities):
            if time.monotonic() >= deadline:
                raise CleanupUnconfirmedError("job process tree still active")
            time.sleep(0.02)

    def descriptor(self):
        return {"backend": self.backend, "scope": self.name}

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
