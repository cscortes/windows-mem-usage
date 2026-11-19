"""Collect running processes and shape them for display."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable, Sequence

from winmem.models import GroupRow, HostInfo, ProcessRow, Snapshot, detect_host

PROCESS_SORTS = ("memory", "private", "name", "pid", "cpu", "user", "percent")
GROUP_SORTS = ("memory", "private", "name", "cpu", "user", "percent", "count")
INSTALLED_SORTS = (
    "memory",
    "private",
    "name",
    "disk",
    "publisher",
    "percent",
    "count",
    "cpu",
)

CPU_SAMPLE_SECONDS = 0.1


class MissingDependency(RuntimeError):
    """psutil is not installed."""


class CollectionError(RuntimeError):
    """Process or memory information could not be read."""


def require_psutil():
    try:
        import psutil
    except ImportError as exc:
        raise MissingDependency(
            "psutil is required. Install it with: py -3 -m pip install psutil"
        ) from exc
    return psutil


def _process_errors():
    errors: list[type[BaseException]] = [OSError]
    try:
        import psutil
    except ImportError:
        return tuple(errors)
    errors.append(psutil.Error)
    return tuple(errors)


def _safe(proc, method: str, default, errors):
    try:
        return getattr(proc, method)()
    except errors:
        return default


def build_snapshot(
    procs: Iterable[object],
    *,
    total: int,
    available: int,
    private_ws: dict[int, int] | None = None,
    sample_cpu: bool = False,
    host: HostInfo | None = None,
) -> Snapshot:
    """Turn process objects into a snapshot.

    Each object follows the bits of ``psutil.Process`` this tool uses:
    ``pid``, ``name``, ``username``, ``status``, ``memory_info``, ``exe``,
    and ``cpu_percent``. Failures on a single process are counted as
    inaccessible and skipped.
    """
    errors = _process_errors()
    private_ws = private_ws or {}
    rows: list[ProcessRow] = []
    skipped = 0
    total = int(total)

    for proc in procs:
        try:
            pid = int(proc.pid)
            mem = proc.memory_info()
            rss = max(int(mem.rss), 0)
        except errors:
            skipped += 1
            continue
        except (TypeError, ValueError, AttributeError):
            skipped += 1
            continue

        shared = getattr(mem, "shared", None)
        private_rss = None
        if shared is not None:
            try:
                private_rss = max(rss - int(shared), 0)
            except (TypeError, ValueError):
                private_rss = None

        private_bytes = getattr(mem, "private", None)
        if private_bytes is not None:
            try:
                private_bytes = max(int(private_bytes), 0)
            except (TypeError, ValueError):
                private_bytes = None

        name = clean_name(_safe(proc, "name", None, errors), pid)
        username = _safe(proc, "username", None, errors)
        if username is not None:
            username = str(username).strip() or None
        status = _safe(proc, "status", None, errors)
        if status is not None:
            status = str(status).strip() or None
        exe = _safe(proc, "exe", None, errors)
        if not exe:
            exe = None
        else:
            exe = str(exe)

        cpu = None
        if sample_cpu:
            try:
                cpu = float(proc.cpu_percent(interval=None))
            except errors:
                cpu = None
            except (TypeError, ValueError):
                cpu = None

        working_set = private_ws.get(pid)
        if working_set is not None:
            working_set = max(int(working_set), 0)

        percent = (rss / total * 100) if total else 0.0
        rows.append(
            ProcessRow(
                pid=pid,
                name=name,
                username=username,
                status=status,
                rss=rss,
                private_bytes=private_bytes,
                private_working_set=working_set,
                private_rss=private_rss,
                percent=percent,
                cpu_percent=cpu,
                exe=exe,
            )
        )

    if host is None:
        host = detect_host(total, available)
    return Snapshot(host=host, rows=tuple(rows), skipped=skipped)


def clean_name(name: object | None, pid: int) -> str:
    text = " ".join(str(name or "").replace("\x00", " ").split())
    return text or f"pid {pid}"


def collect_snapshot(*, sample_cpu: bool = True, cpu_delay: float = CPU_SAMPLE_SECONDS) -> Snapshot:
    """Read a live snapshot from this computer."""
    psutil = require_psutil()
    try:
        procs = list(psutil.process_iter())
    except psutil.Error as exc:
        raise CollectionError(f"could not list processes: {exc}") from exc

    if sample_cpu and cpu_delay > 0:
        errors = _process_errors()
        for proc in procs:
            try:
                proc.cpu_percent(interval=None)
            except errors:
                continue
        time.sleep(cpu_delay)

    try:
        memory = psutil.virtual_memory()
        cpus = psutil.cpu_count(logical=True)
    except psutil.Error as exc:
        raise CollectionError(f"could not read memory: {exc}") from exc

    private_ws: dict[int, int] = {}
    product_name = None
    display_version = None
    if sys.platform == "win32":
        from winmem.winapi import read_private_working_sets, read_windows_identity

        pids: list[int] = []
        for proc in procs:
            try:
                pids.append(int(proc.pid))
            except (psutil.Error, OSError, TypeError, ValueError):
                continue
        private_ws = read_private_working_sets(pids)
        product_name, display_version = read_windows_identity()

    host = detect_host(
        memory.total,
        memory.available,
        cpus=cpus,
        product_name=product_name,
        display_version=display_version,
    )
    return build_snapshot(
        procs,
        total=memory.total,
        available=memory.available,
        private_ws=private_ws,
        sample_cpu=sample_cpu,
        host=host,
    )


def choose_private_attr(rows: Sequence[object]) -> str:
    """Pick the private-memory field that this set of rows can actually show."""
    for attr in ("private_working_set", "private_rss", "private_bytes"):
        if any(getattr(row, attr, None) is not None for row in rows):
            return attr
    return "private_bytes"


def _tie(row: object) -> int:
    pid = getattr(row, "pid", None)
    if isinstance(pid, int):
        return pid
    lead = getattr(row, "lead_pid", 0)
    return lead if isinstance(lead, int) else 0


def sort_rows(rows: Sequence[object], field: str, *, descending: bool, private_attr: str) -> list:
    """Sort process, group, or installed rows.

    Numeric sorts put the name A to Z as the tie break. Equal resident
    memory therefore stays alphabetical in either direction of the primary key.
    """

    def key(row):
        name = str(getattr(row, "name", "")).casefold()
        tie = _tie(row)
        if field == "memory":
            number = row.rss
        elif field == "percent":
            number = row.percent
        elif field == "private":
            raw = getattr(row, private_attr, None)
            number = raw if raw is not None else -1
        elif field == "cpu":
            number = row.cpu_percent if row.cpu_percent is not None else -1.0
        elif field == "pid":
            number = row.pid
        elif field == "count":
            number = row.process_count
        elif field == "disk":
            size = row.estimated_size_kb
            number = size if size is not None else -1
        elif field == "name":
            return (name, tie)
        elif field == "user":
            return ((row.username or "").casefold(), name, tie)
        elif field == "publisher":
            return ((getattr(row, "publisher", None) or "").casefold(), name, tie)
        else:
            raise ValueError(f"unknown sort field: {field}")
        directed = -number if descending else number
        return (directed, name, tie)

    text_fields = {"name", "user", "publisher"}
    return sorted(rows, key=key, reverse=descending if field in text_fields else False)


def filter_processes(
    rows: Sequence[ProcessRow],
    *,
    name: str | None,
    user: str | None,
) -> list[ProcessRow]:
    needle = (name or "").casefold()
    user_needle = (user or "").casefold()
    matched: list[ProcessRow] = []
    for row in rows:
        if needle:
            haystack = f"{row.name} {row.exe or ''}".casefold()
            if needle not in haystack:
                continue
        if user_needle and user_needle not in (row.username or "").casefold():
            continue
        matched.append(row)
    return matched


def apply_min_memory(rows: Sequence[object], min_bytes: int) -> list:
    if min_bytes <= 0:
        return list(rows)
    return [row for row in rows if int(row.rss) >= min_bytes]


def group_rows(rows: Sequence[ProcessRow], total: int) -> list[GroupRow]:
    """Sum processes that share an executable name."""
    order: list[str] = []
    buckets: dict[str, list[ProcessRow]] = {}
    labels: dict[str, str] = {}
    for row in rows:
        key = row.name.casefold()
        if key not in buckets:
            buckets[key] = []
            labels[key] = row.name
            order.append(key)
        buckets[key].append(row)

    grouped: list[GroupRow] = []
    for key in order:
        members = buckets[key]
        rss = sum(member.rss for member in members)
        users = sorted({member.username for member in members if member.username})
        if not users:
            username = None
        elif len(users) == 1:
            username = users[0]
        else:
            username = f"{len(users)} users"
        pids = tuple(sorted(member.pid for member in members))
        cpus = [member.cpu_percent for member in members if member.cpu_percent is not None]
        grouped.append(
            GroupRow(
                name=labels[key],
                username=username,
                process_count=len(members),
                rss=rss,
                private_bytes=_sum_same(members, "private_bytes"),
                private_working_set=_sum_same(members, "private_working_set"),
                private_rss=_sum_same(members, "private_rss"),
                percent=(rss / total * 100) if total else 0.0,
                cpu_percent=sum(cpus) if cpus else None,
                pids=pids,
                lead_pid=pids[0] if pids else 0,
            )
        )
    return grouped


def _sum_same(members: Sequence[object], attr: str) -> int | None:
    values = [getattr(member, attr, None) for member in members]
    if not values or any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def sum_attr(rows: Sequence[object], attr: str) -> int | None:
    if not rows:
        return 0 if attr == "rss" else None
    return _sum_same(rows, attr)


def descending_by_default(field: str) -> bool:
    return field not in {"name", "user", "publisher"}
