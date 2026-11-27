"""Start AGY inside a Windows Job before it can spawn any descendants."""

import ctypes
from ctypes import wintypes
import subprocess
import sys
import uuid


def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, result, args in (
        ("CreateJobObjectW", wintypes.HANDLE, (ctypes.c_void_p, wintypes.LPCWSTR)),
        ("OpenJobObjectW", wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)),
        ("GetCurrentProcess", wintypes.HANDLE, ()),
        ("AssignProcessToJobObject", wintypes.BOOL, (wintypes.HANDLE, wintypes.HANDLE)),
        ("SetInformationJobObject", wintypes.BOOL,
         (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)),
        ("CloseHandle", wintypes.BOOL, (wintypes.HANDLE,)),
    ):
        function = getattr(api, name)
        function.restype = result
        function.argtypes = args
    return api


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("max_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
        ("memory_limits", ctypes.c_size_t * 4),
    ]


class WindowsJob:
    def __init__(self):
        self.api = _kernel()
        self.name = f"Local\\agy-bridge-{uuid.uuid4()}"
        self.handle = self.api.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def wrap(self, argv):
        return [sys.executable, "-I", __file__, self.name, *argv]

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


if __name__ == "__main__":
    api = _kernel()
    job = api.OpenJobObjectW(0x0001, False, sys.argv[1])  # JOB_OBJECT_ASSIGN_PROCESS
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(job)
    raise SystemExit(subprocess.call(sys.argv[2:]))
