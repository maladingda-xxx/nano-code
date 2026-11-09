"""Start Windows commands suspended so their descendants stay in one job."""

import _winapi
import ctypes
import msvcrt
import subprocess
import sys
from contextlib import contextmanager
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process_time", ctypes.c_longlong),
        ("per_job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
_kernel.CreateJobObjectW.restype = wintypes.HANDLE
_kernel.SetInformationJobObject.argtypes = (
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
)
_kernel.SetInformationJobObject.restype = wintypes.BOOL
_kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_kernel.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel.ResumeThread.argtypes = (wintypes.HANDLE,)
_kernel.ResumeThread.restype = wintypes.DWORD

_CREATE_SUSPENDED = 0x00000004
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_KILL_ON_JOB_CLOSE = 0x00002000


@contextmanager
def start_process(argv, cwd, stdin, stdout, stderr):
    job = _kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = thread = None
    assigned = False
    handles = []
    try:
        limits = _ExtendedLimits()
        limits.basic.flags = _KILL_ON_JOB_CLOSE
        if not _kernel.SetInformationJobObject(
            job, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        current_process = _winapi.GetCurrentProcess()
        try:
            for stream in (stdin, stdout, stderr):
                handles.append(_winapi.DuplicateHandle(
                    current_process, msvcrt.get_osfhandle(stream.fileno()), current_process,
                    0, True, _winapi.DUPLICATE_SAME_ACCESS,
                ))
            startup = subprocess.STARTUPINFO(
                dwFlags=subprocess.STARTF_USESTDHANDLES,
                hStdInput=handles[0], hStdOutput=handles[1], hStdError=handles[2],
                lpAttributeList={"handle_list": handles},
            )
            command_line = subprocess.list2cmdline(argv)
            sys.audit("subprocess.Popen", None, command_line, str(cwd), None)
            process, thread, _, _ = _winapi.CreateProcess(
                None, command_line, None, None, True,
                _CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP, None, str(cwd), startup,
            )
        finally:
            for handle in handles:
                _winapi.CloseHandle(handle)

        # User code can run only after the process is attached to the job.
        if not _kernel.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
        assigned = True
        if _kernel.ResumeThread(thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())

        def poll():
            if _winapi.WaitForSingleObject(process, 0) == _winapi.WAIT_OBJECT_0:
                return _winapi.GetExitCodeProcess(process)
            return None

        yield poll
    finally:
        try:
            if process is not None and not assigned:
                _winapi.TerminateProcess(process, 1)
        finally:
            _winapi.CloseHandle(job)
            if thread is not None:
                _winapi.CloseHandle(thread)
            if process is not None:
                try:
                    _winapi.WaitForSingleObject(process, 5000)
                finally:
                    _winapi.CloseHandle(process)
