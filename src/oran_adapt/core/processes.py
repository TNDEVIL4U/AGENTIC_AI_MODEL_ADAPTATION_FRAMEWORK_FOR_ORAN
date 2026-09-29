"""Process-tree helpers shared by the sandbox runner and the job executors, with no
dependency beyond the standard library and no POSIX-only calls on Windows.

``descendants(pid)`` lists every live process below ``pid``; ``terminate(pid)`` kills one
process (TerminateProcess on Windows, SIGKILL on Linux); ``kill_tree(pid)`` kills a process and
everything below it, children first; ``pid_alive(pid)`` probes without signalling (on Windows
``os.kill(pid, 0)`` would send CTRL_C_EVENT, not probe); ``rss_bytes(pid)`` reads resident
memory. Only ever call them on processes this program started.
"""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TH32CS_SNAPPROCESS = 0x2
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    def parent_map() -> dict[int, int]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == _INVALID_HANDLE:
            return {}
        try:
            parents: dict[int, int] = {}
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return parents
        finally:
            _kernel32.CloseHandle(snapshot)

    def terminate(pid: int) -> None:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                _kernel32.TerminateProcess(handle, 1)
            finally:
                _kernel32.CloseHandle(handle)

    def rss_bytes(pid: int) -> int | None:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not _kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize)
        finally:
            _kernel32.CloseHandle(handle)

    def pid_alive(pid: int) -> bool:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

elif sys.platform.startswith("linux"):
    import signal

    def parent_map() -> dict[int, int]:
        parents: dict[int, int] = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="ascii", errors="replace") as f:
                    # "pid (comm) state ppid ..." - comm may itself contain spaces or ")".
                    fields = f.read().rpartition(")")[2].split()
                parents[int(name)] = int(fields[1])
            except (OSError, ValueError, IndexError):
                continue
        return parents

    def terminate(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def rss_bytes(pid: int) -> int | None:
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            return None
        return None

    def pid_alive(pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
                state = f.read().rpartition(")")[2].split()[0]
        except (OSError, IndexError):
            return False
        return state not in ("Z", "X")  # a zombie has exited; only its exit status remains

else:

    def parent_map() -> dict[int, int]:
        return {}

    def terminate(pid: int) -> None:
        return None

    def rss_bytes(pid: int) -> int | None:
        return None  # no portable RSS source without a new dependency: limit not enforced

    def pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)  # POSIX only: signal 0 probes without delivering anything
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def descendants(root: int) -> list[int]:
    """Every live process below ``root``. On Windows a venv's python.exe is a launcher that runs
    the real interpreter as its child, so the sandbox's memory lives one level down."""
    children: dict[int, list[int]] = {}
    for pid, ppid in parent_map().items():
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    queue = list(children.get(root, []))
    while queue:
        pid = queue.pop()
        found.append(pid)
        queue.extend(children.get(pid, []))
    return found


def kill_tree(pid: int) -> None:
    """Kill ``pid`` and every process below it, the deepest first so none is re-parented
    away. A venv's python.exe on Windows is a launcher: killing only it would leave the real
    interpreter running."""
    for child in reversed(descendants(pid)):
        terminate(child)
    terminate(pid)
