"""Windows-only memory and registry readers.

The private working set comes from ``PROCESS_MEMORY_COUNTERS_EX2``, which
Windows 11 22H2 (September 2023 update and later) returns from
``GetProcessMemoryInfo``. That value is the Memory column in Task Manager.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterable

# PROCESS_QUERY_LIMITED_INFORMATION. Enough for GetProcessMemoryInfo.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87

UNINSTALL_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_WINDOWS_VERSION_PATH = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"


class PROCESS_MEMORY_COUNTERS_EX2(ctypes.Structure):
    """Layout from psapi.h. Sizes follow the process pointer width."""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
        ("PrivateWorkingSetSize", ctypes.c_size_t),
        ("SharedCommitUsage", ctypes.c_uint64),
    ]


def read_private_working_sets(pids: Iterable[int]) -> dict[int, int]:
    """Map pid to private working set, in bytes.

    Returns an empty dict off Windows, when the structure is rejected by an
    older system, or when no process can be queried. Missing pids are omitted
    rather than reported as zero.
    """
    if sys.platform != "win32":
        return {}
    try:
        api = _memory_api()
    except OSError:
        return {}
    if api is None:
        return {}
    kernel32, query = api
    found: dict[int, int] = {}
    for pid in pids:
        try:
            value, error = _query_private_working_set(kernel32, query, int(pid))
        except OSError:
            continue
        if error == _ERROR_INVALID_PARAMETER and not found:
            return {}
        if error == _ERROR_INVALID_PARAMETER:
            break
        if value is not None:
            found[int(pid)] = value
    return found


def _memory_api():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    query = None
    for library_name, function_name in (
        ("kernel32", "K32GetProcessMemoryInfo"),
        ("psapi", "GetProcessMemoryInfo"),
    ):
        try:
            library = ctypes.WinDLL(library_name, use_last_error=True)
            candidate = getattr(library, function_name)
        except (OSError, AttributeError):
            continue
        candidate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX2),
            ctypes.c_uint32,
        ]
        candidate.restype = ctypes.c_int
        query = candidate
        break
    if query is None:
        return None
    return kernel32, query


def _query_private_working_set(kernel32, query, pid: int) -> tuple[int | None, int | None]:
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None, ctypes.get_last_error()
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX2()
        counters.cb = ctypes.sizeof(counters)
        ok = query(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None, ctypes.get_last_error()
        return int(counters.PrivateWorkingSetSize), None
    finally:
        kernel32.CloseHandle(handle)


def read_windows_identity() -> tuple[str | None, str | None]:
    """Return ``(ProductName, DisplayVersion)`` from the Windows registry."""
    if sys.platform != "win32":
        return None, None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_VERSION_PATH) as key:
            product = _reg_value(winreg, key, "ProductName")
            display = _reg_value(winreg, key, "DisplayVersion")
    except OSError:
        return None, None
    return _as_text(product), _as_text(display)


def enum_uninstall_entries() -> list[dict]:
    """Read uninstall registry values from the machine and current user.

    32-bit and 64-bit views are both opened so a 32-bit Python still sees
    64-bit apps, and the reverse.
    """
    if sys.platform != "win32":
        return []
    import winreg

    specs = (
        (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_PATH, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_PATH, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, UNINSTALL_PATH, 0),
    )
    entries: list[dict] = []
    for root, path, view in specs:
        entries.extend(_enum_tree(winreg, root, path, view))
    return entries


def _enum_tree(winreg, root, path: str, view: int) -> list[dict]:
    access = winreg.KEY_READ | view
    try:
        key = winreg.OpenKey(root, path, 0, access)
    except OSError:
        return []
    entries: list[dict] = []
    try:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            try:
                sub = winreg.OpenKey(key, name, 0, access)
            except OSError:
                continue
            try:
                entries.append(_read_values(winreg, sub))
            finally:
                winreg.CloseKey(sub)
    finally:
        winreg.CloseKey(key)
    return entries


def _read_values(winreg, key) -> dict:
    values: dict = {}
    index = 0
    while True:
        try:
            name, data, _kind = winreg.EnumValue(key, index)
        except OSError:
            break
        values[name] = data
        index += 1
    return values


def _reg_value(winreg, key, name: str):
    try:
        data, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return data


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace").strip()
    else:
        text = str(value).strip()
    return text or None
