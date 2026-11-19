"""Break RAM in use into pieces that add back up to the system total.

Task Manager's Memory column is private RAM. Shared pages, kernel pools, and
memory not tied to one process are inside "in use" and absent from that column,
so adding the column up comes out short. Adding full working sets counts each
shared page once per process and comes out high. The lines returned here are
chosen so they sum to in-use RAM and to available RAM with nothing left over.
"""

from __future__ import annotations

from dataclasses import dataclass

from winmem.models import Snapshot


@dataclass(frozen=True)
class MemoryFacts:
    total: int
    available: int
    system: str
    process_private: int
    process_count: int
    working_set: int
    skipped_processes: int
    nonpaged_pool: int | None = None
    paged_pool_resident: int | None = None
    system_code_resident: int | None = None
    system_driver_resident: int | None = None
    system_cache_resident: int | None = None
    modified: int | None = None
    standby: int | None = None
    free_bytes: int | None = None
    kernel_stack: int | None = None
    page_tables: int | None = None
    unreclaimable_slab: int | None = None
    commit: int | None = None
    commit_limit: int | None = None
    installed: int | None = None


@dataclass(frozen=True)
class LedgerLine:
    key: str
    label: str
    amount: int


@dataclass(frozen=True)
class Ledger:
    total: int
    in_use: int
    available: int
    system: str
    in_use_lines: tuple[LedgerLine, ...]
    available_lines: tuple[LedgerLine, ...]
    working_set: int
    process_private_measured: int
    commit: int | None
    commit_limit: int | None
    hardware_reserved: int | None
    skipped_processes: int
    omitted: tuple[str, ...]
    available_was_scaled: bool

    def closes(self) -> bool:
        in_sum = sum(line.amount for line in self.in_use_lines)
        available_sum = sum(line.amount for line in self.available_lines)
        return (
            in_sum == self.in_use
            and available_sum == self.available
            and self.in_use + self.available == self.total
        )


def facts_from_snapshot(snapshot: Snapshot) -> MemoryFacts:
    """Sum process memory and read kernel counters for this computer."""
    private = 0
    working = 0
    counted = 0
    windows = snapshot.host.is_windows
    for row in snapshot.rows:
        working += max(int(row.rss), 0)
        value = row.private_working_set if windows else row.private_rss
        if value is None:
            continue
        private += max(int(value), 0)
        counted += 1
    extra = read_platform_counters()
    return MemoryFacts(
        total=max(int(snapshot.host.total), 0),
        available=max(int(snapshot.host.available), 0),
        system=snapshot.host.system,
        process_private=private,
        process_count=counted,
        working_set=working,
        skipped_processes=snapshot.skipped,
        nonpaged_pool=_counter(extra, "nonpaged_pool"),
        paged_pool_resident=_counter(extra, "paged_pool_resident"),
        system_code_resident=_counter(extra, "system_code_resident"),
        system_driver_resident=_counter(extra, "system_driver_resident"),
        system_cache_resident=_counter(extra, "system_cache_resident"),
        modified=_counter(extra, "modified"),
        standby=_counter(extra, "standby"),
        free_bytes=_counter(extra, "free_bytes"),
        kernel_stack=_counter(extra, "kernel_stack"),
        page_tables=_counter(extra, "page_tables"),
        unreclaimable_slab=_counter(extra, "unreclaimable_slab"),
        commit=_counter(extra, "commit"),
        commit_limit=_counter(extra, "commit_limit"),
        installed=_counter(extra, "installed"),
    )


def read_platform_counters() -> dict[str, int]:
    """Kernel memory counters. Empty when this operating system has none."""
    try:
        from winmem.counters import read_memory_counters
    except ImportError:
        return {}
    try:
        return read_memory_counters()
    except OSError:
        return {}


def build_ledger(facts: MemoryFacts) -> Ledger:
    total = max(int(facts.total), 0)
    available = min(max(int(facts.available), 0), total)
    in_use = total - available
    measured_private = max(int(facts.process_private), 0)
    private = min(measured_private, in_use)
    count = max(int(facts.process_count), 0)
    if count == 1:
        private_label = "Process private RAM (1 process)"
    elif count:
        private_label = f"Process private RAM ({count} processes)"
    else:
        private_label = "Process private RAM"

    candidates = (
        ("process_private", private_label, private, 10),
        ("modified", "Modified pages waiting to be written", facts.modified, 20),
        ("nonpaged_pool", "Non-paged pool (kernel RAM kept in memory)", facts.nonpaged_pool, 30),
        ("kernel_stack", "Kernel stacks", facts.kernel_stack, 40),
        ("page_tables", "Page tables", facts.page_tables, 50),
        ("unreclaimable_slab", "Unreclaimable kernel slab", facts.unreclaimable_slab, 60),
        ("paged_pool_resident", "Paged pool in RAM", facts.paged_pool_resident, 70),
        ("system_code_resident", "Windows kernel code in RAM", facts.system_code_resident, 80),
        ("system_driver_resident", "Drivers in RAM", facts.system_driver_resident, 90),
        ("system_cache_resident", "Active file cache", facts.system_cache_resident, 100),
    )
    in_use_lines, omitted = _fit_in_use(in_use, candidates)
    available_lines, scaled = _fit_available(available, facts.standby, facts.free_bytes)
    reserved = None
    if facts.installed is not None and facts.installed > total:
        reserved = int(facts.installed) - total
    return Ledger(
        total=total,
        in_use=in_use,
        available=available,
        system=facts.system,
        in_use_lines=tuple(in_use_lines),
        available_lines=tuple(available_lines),
        working_set=max(int(facts.working_set), 0),
        process_private_measured=measured_private,
        commit=facts.commit if facts.commit is not None and facts.commit >= 0 else None,
        commit_limit=facts.commit_limit if facts.commit_limit is not None and facts.commit_limit > 0 else None,
        hardware_reserved=reserved,
        skipped_processes=max(int(facts.skipped_processes), 0),
        omitted=tuple(omitted),
        available_was_scaled=scaled,
    )


def _fit_in_use(budget: int, candidates: tuple[tuple[str, str, int | None, int], ...]) -> tuple[list[LedgerLine], list[str]]:
    if budget <= 0:
        return [], []
    remaining = budget
    lines: list[LedgerLine] = []
    omitted: list[str] = []
    ordered = sorted(candidates, key=lambda item: item[3])
    for key, label, amount, _priority in ordered:
        if amount is None:
            continue
        amount = int(amount)
        if amount <= 0:
            continue
        if amount > remaining:
            omitted.append(label)
            continue
        lines.append(LedgerLine(key, label, amount))
        remaining -= amount
    if remaining > 0:
        lines.append(LedgerLine("other", "Other memory in use", remaining))
    return lines, omitted


def _fit_available(
    budget: int,
    standby: int | None,
    free_bytes: int | None,
) -> tuple[list[LedgerLine], bool]:
    budget = max(budget, 0)
    known: list[tuple[str, str, int]] = []
    if standby is not None and standby > 0:
        known.append(("standby", "Standby cache (ready for a program)", int(standby)))
    if free_bytes is not None and free_bytes > 0:
        known.append(("free", "Free", int(free_bytes)))
    if not known:
        if budget:
            return [LedgerLine("available", "Available", budget)], False
        return [], False
    known_total = sum(amount for _key, _label, amount in known)
    # A megabyte of disagreement is sampling noise between two counters, not a real bucket.
    slack = 1024 * 1024
    if abs(known_total - budget) <= slack:
        lines = [LedgerLine(key, label, amount) for key, label, amount in known]
        if lines and known_total != budget:
            last = lines[-1]
            lines[-1] = LedgerLine(last.key, last.label, last.amount + (budget - known_total))
            if lines[-1].amount <= 0:
                lines.pop()
        if sum(line.amount for line in lines) != budget:
            lines.append(LedgerLine("available_other", "Other available memory", budget - sum(line.amount for line in lines)))
        return [line for line in lines if line.amount > 0], False
    if known_total < budget:
        lines = [LedgerLine(key, label, amount) for key, label, amount in known]
        lines.append(LedgerLine("available_other", "Other available memory", budget - known_total))
        return lines, False
    # The counters were sampled a moment apart and overshoot the available total.
    lines = []
    left = budget
    for index, (key, label, amount) in enumerate(known):
        if index == len(known) - 1:
            take = left
        else:
            take = budget * amount // known_total
            take = min(take, left)
        if take > 0:
            lines.append(LedgerLine(key, label, take))
            left -= take
    return lines, True


def _counter(values: dict[str, int], key: str) -> int | None:
    if key not in values:
        return None
    try:
        amount = int(values[key])
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return amount
