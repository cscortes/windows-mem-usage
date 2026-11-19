"""Kernel memory counters that are not attached to one process.

Windows counters come from the documented performance-counter provider and
from GetPerformanceInfo. Linux counters come from /proc/meminfo. Either source
can be missing; the ledger still adds up by putting the unknown part in
"Other memory in use".
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def read_memory_counters() -> dict[str, int]:
    if sys.platform == "win32":
        return _windows_counters()
    if sys.platform.startswith("linux"):
        return _linux_counters()
    return {}


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo into byte counts."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.split()
        if not parts or not parts[0].isdigit():
            continue
        amount = int(parts[0])
        if len(parts) > 1 and parts[1].lower() == "kb":
            amount *= 1024
        values[key.strip()] = amount
    return values


def balloon_bytes(inflate: int, deflate: int, page_size: int) -> int:
    """RAM the hypervisor is holding, from the balloon event counters in /proc/vmstat."""
    pages = max(0, int(inflate) - int(deflate))
    return pages * max(int(page_size), 0)


def file_cache_in_use(cached: int, buffers: int, available: int, free: int) -> int:
    """File cache that is not already counted as available memory."""
    reclaimable = max(0, int(available) - int(free))
    return max(0, int(cached) + int(buffers) - reclaimable)


def _linux_counters() -> dict[str, int]:
    text = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
    info = parse_meminfo(text)
    mapping = {
        "kernel_stack": "KernelStack",
        "page_tables": "PageTables",
        "unreclaimable_slab": "SUnreclaim",
    }
    counters: dict[str, int] = {}
    for field, key in mapping.items():
        if key in info and info[key] >= 0:
            counters[field] = info[key]
    events = _vmstat()
    if "balloon_inflate" in events:
        page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        held = balloon_bytes(events.get("balloon_inflate", 0), events.get("balloon_deflate", 0), page or 4096)
        if held > 0:
            counters["balloon"] = held
    needed = ("Cached", "Buffers", "MemAvailable", "MemFree")
    if all(key in info for key in needed):
        held_cache = file_cache_in_use(info["Cached"], info["Buffers"], info["MemAvailable"], info["MemFree"])
        if held_cache > 0:
            counters["system_cache_resident"] = held_cache
    return counters


def _vmstat() -> dict[str, int]:
    values: dict[str, int] = {}
    text = Path("/proc/vmstat").read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            values[parts[0]] = int(parts[1])
    return values


def _windows_counters() -> dict[str, int]:
    counters: dict[str, int] = {}
    counters.update(_pdh_memory_counters())
    counters.update(_performance_info_fallback(counters))
    installed = _installed_ram()
    if installed is not None:
        counters["installed"] = installed
    return counters


_PDH_COUNTERS = {
    "nonpaged_pool": r"\Memory\Pool Nonpaged Bytes",
    "paged_pool_resident": r"\Memory\Pool Paged Resident Bytes",
    "system_code_resident": r"\Memory\System Code Resident Bytes",
    "system_driver_resident": r"\Memory\System Driver Resident Bytes",
    "system_cache_resident": r"\Memory\System Cache Resident Bytes",
    "modified": r"\Memory\Modified Page List Bytes",
    "free_bytes": r"\Memory\Free & Zero Page List Bytes",
    "commit": r"\Memory\Committed Bytes",
    "commit_limit": r"\Memory\Commit Limit",
}
_PDH_STANDBY = (
    r"\Memory\Standby Cache Core Bytes",
    r"\Memory\Standby Cache Normal Priority Bytes",
    r"\Memory\Standby Cache Reserve Bytes",
)
# A raw counter value is usable when PDH reports one of these statuses.
_PDH_OK = {0, 1}
_PDH_FMT_LARGE = 0x00000400


class _PdhValue(ctypes.Union):
    _fields_ = [
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("ansi", ctypes.c_char_p),
        ("wide", ctypes.c_wchar_p),
    ]


class _PdhFormatted(ctypes.Structure):
    _fields_ = [
        ("CStatus", ctypes.c_ulong),
        ("value", _PdhValue),
    ]


def _pdh_memory_counters() -> dict[str, int]:
    try:
        pdh = ctypes.WinDLL("pdh", use_last_error=True)
    except OSError:
        return {}
    try:
        _bind_pdh(pdh)
    except AttributeError:
        return {}
    query = ctypes.c_void_p()
    if pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
        return {}
    try:
        handles: dict[str, ctypes.c_void_p] = {}
        standby_handles: list[ctypes.c_void_p] = []
        for key, path in _PDH_COUNTERS.items():
            handle = _add_counter(pdh, query, path)
            if handle is not None:
                handles[key] = handle
        for path in _PDH_STANDBY:
            handle = _add_counter(pdh, query, path)
            if handle is not None:
                standby_handles.append(handle)
        if pdh.PdhCollectQueryData(query) != 0:
            return {}
        values: dict[str, int] = {}
        for key, handle in handles.items():
            amount = _counter_value(pdh, handle)
            if amount is not None:
                values[key] = amount
        standby_parts = [amount for handle in standby_handles if (amount := _counter_value(pdh, handle)) is not None]
        if standby_parts:
            values["standby"] = sum(standby_parts)
        return values
    finally:
        pdh.PdhCloseQuery(query)


def _bind_pdh(pdh) -> None:
    pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    pdh.PdhOpenQueryW.restype = ctypes.c_long
    pdh.PdhAddEnglishCounterW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
    pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    pdh.PdhCollectQueryData.restype = ctypes.c_long
    pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
    pdh.PdhCloseQuery.restype = ctypes.c_long
    pdh.PdhGetFormattedCounterValue.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(_PdhFormatted),
    ]
    pdh.PdhGetFormattedCounterValue.restype = ctypes.c_long


def _add_counter(pdh, query, path: str):
    handle = ctypes.c_void_p()
    if pdh.PdhAddEnglishCounterW(query, path, None, ctypes.byref(handle)) != 0:
        return None
    return handle


def _counter_value(pdh, handle) -> int | None:
    formatted = _PdhFormatted()
    kind = ctypes.c_ulong()
    if pdh.PdhGetFormattedCounterValue(handle, _PDH_FMT_LARGE, ctypes.byref(kind), ctypes.byref(formatted)) != 0:
        return None
    if int(formatted.CStatus) not in _PDH_OK:
        return None
    amount = int(formatted.value.largeValue)
    if amount < 0:
        return None
    return amount


class _PerformanceInformation(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", ctypes.c_ulong),
        ("ProcessCount", ctypes.c_ulong),
        ("ThreadCount", ctypes.c_ulong),
    ]


def _performance_info_fallback(already: dict[str, int]) -> dict[str, int]:
    """Fill kernel pool and commit from psapi when a performance counter is missing."""
    needed = ("nonpaged_pool", "commit", "commit_limit")
    if all(key in already for key in needed):
        return {}
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except OSError:
        return {}
    psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(_PerformanceInformation), ctypes.c_ulong]
    psapi.GetPerformanceInfo.restype = ctypes.c_int
    info = _PerformanceInformation()
    info.cb = ctypes.sizeof(info)
    if not psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        return {}
    page = int(info.PageSize) or 0
    if page <= 0:
        return {}
    filled: dict[str, int] = {}
    if "nonpaged_pool" not in already:
        filled["nonpaged_pool"] = int(info.KernelNonpaged) * page
    if "commit" not in already:
        filled["commit"] = int(info.CommitTotal) * page
    if "commit_limit" not in already:
        filled["commit_limit"] = int(info.CommitLimit) * page
    return filled


def _installed_ram() -> int | None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None
    kernel32.GetPhysicallyInstalledSystemMemory.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
    kernel32.GetPhysicallyInstalledSystemMemory.restype = ctypes.c_int
    kilobytes = ctypes.c_ulonglong()
    if not kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kilobytes)):
        return None
    amount = int(kilobytes.value) * 1024
    if amount <= 0:
        return None
    return amount
