"""Match Windows installed programs to the processes that are running them."""

from __future__ import annotations

import platform
import sys
from collections.abc import Sequence
from pathlib import PureWindowsPath

from winmem.models import InstalledMemory, InstalledProgram, ProcessRow, clean

SKIP_RELEASE_TYPES = {
    "update",
    "hotfix",
    "security update",
    "update rollup",
    "service pack",
    "security hotfix",
    "security rollup",
}

# Icon names that belong to Windows itself or to a shared installer, not to
# one application. Matching on these would attribute unrelated processes.
GENERIC_ICON_NAMES = {
    "msiexec.exe",
    "rundll32.exe",
    "explorer.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "dllhost.exe",
    "svchost.exe",
    "openwith.exe",
    "control.exe",
    "mmc.exe",
    "conhost.exe",
    "sihost.exe",
    "taskhostw.exe",
    "runtimebroker.exe",
    "applicationframehost.exe",
    "systemsettings.exe",
    "backgroundtaskhost.exe",
    "wwahost.exe",
    "unins000.exe",
    "uninstall.exe",
    "setup.exe",
    "install.exe",
    "update.exe",
    "updater.exe",
}


class InstalledProgramsUnavailable(RuntimeError):
    """``--installed`` was asked for on a system without the Windows registry."""

    def __init__(self, system: str):
        super().__init__(
            "--installed reads the Windows uninstall registry. "
            f"This computer is {system}. Run that option on Windows 11."
        )


def load_installed_programs() -> list[InstalledProgram]:
    if sys.platform != "win32":
        raise InstalledProgramsUnavailable(platform.system() or sys.platform)
    from winmem.winapi import enum_uninstall_entries

    programs: list[InstalledProgram] = []
    for entry in enum_uninstall_entries():
        parsed = parse_uninstall_entry(entry)
        if parsed is not None:
            programs.append(parsed)
    return dedupe_programs(programs)


def parse_uninstall_entry(values: dict) -> InstalledProgram | None:
    """Turn one uninstall registry key into a program, or skip non-apps."""
    name = clean(_text(values.get("DisplayName")))
    if not name:
        return None
    if _is_true(values.get("SystemComponent")):
        return None
    if clean(_text(values.get("ParentKeyName"))) or clean(_text(values.get("ParentDisplayName"))):
        return None
    release = clean(_text(values.get("ReleaseType"))).casefold()
    if release in SKIP_RELEASE_TYPES:
        return None
    lowered = name.casefold()
    if lowered.startswith("security update") or lowered.startswith("hotfix for"):
        return None

    version = clean(_text(values.get("DisplayVersion"))) or None
    publisher = clean(_text(values.get("Publisher"))) or None
    location = clean(_text(values.get("InstallLocation"))) or None
    _icon_path, icon_name = split_icon(values.get("DisplayIcon"))
    if icon_name and icon_name.casefold() in GENERIC_ICON_NAMES:
        icon_name = None
    return InstalledProgram(
        name=name,
        version=version,
        publisher=publisher,
        install_location=location,
        icon_name=icon_name,
        estimated_size_kb=parse_kb(values.get("EstimatedSize")),
    )


def dedupe_programs(programs: Sequence[InstalledProgram]) -> list[InstalledProgram]:
    """Drop the same app when it appears in both registry views."""
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[InstalledProgram] = []
    for program in programs:
        key = (
            program.name.casefold(),
            (program.version or "").casefold(),
            (program.publisher or "").casefold(),
            (program.install_location or "").casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(program)
    return unique


def split_icon(display_icon: object | None) -> tuple[str | None, str | None]:
    """Split a DisplayIcon value into a path and a file name.

    Registry icons look like ``C:\\App\\app.exe,0``. The trailing comma and
    icon index are not part of the path.
    """
    raw = clean(_text(display_icon)).strip('"')
    if not raw:
        return None, None
    head, separator, tail = raw.rpartition(",")
    if separator and tail.strip().lstrip("-").isdigit():
        raw = head.strip().strip('"')
    if not raw:
        return None, None
    path = PureWindowsPath(raw)
    name = path.name
    if not name or name in {".", ".."}:
        return None, None
    return str(path), name


def parse_kb(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip().replace(",", "")
    if text.isdigit():
        return int(text)
    return None


def attach_process_memory(
    programs: Sequence[InstalledProgram],
    processes: Sequence[ProcessRow],
    total: int,
) -> list[InstalledMemory]:
    """Add the memory of running processes onto each installed program.

    A process is charged to the program with the most specific install folder
    that contains its executable. If no folder matches, and exactly one
    program has the same icon file name, the process is charged to that program.
    Each process is counted once.
    """
    locations: list[tuple[int, int, PureWindowsPath]] = []
    for index, program in enumerate(programs):
        path = _location_path(program.install_location)
        if path is None or is_broad_location(path):
            continue
        locations.append((len(str(path)), index, path))
    locations.sort(key=lambda item: item[0], reverse=True)

    assigned: dict[int, int] = {}
    for proc in processes:
        exe = _exe_path(proc.exe)
        if exe is None:
            continue
        for _length, index, path in locations:
            if is_under(exe, path):
                assigned[proc.pid] = index
                break

    icon_owners: dict[str, list[int]] = {}
    for index, program in enumerate(programs):
        icon = (program.icon_name or "").casefold()
        if not icon or icon in GENERIC_ICON_NAMES:
            continue
        icon_owners.setdefault(icon, []).append(index)
        if icon.endswith(".exe"):
            icon_owners.setdefault(icon[:-4], []).append(index)
    unique_icons = {
        name: owners[0]
        for name, owners in icon_owners.items()
        if len(owners) == 1
    }

    for proc in processes:
        if proc.pid in assigned:
            continue
        owner = None
        for key in sorted(_image_keys(proc), key=len, reverse=True):
            if key in unique_icons:
                owner = unique_icons[key]
                break
        if owner is not None:
            assigned[proc.pid] = owner

    by_program: dict[int, list[ProcessRow]] = {}
    for proc in processes:
        index = assigned.get(proc.pid)
        if index is None:
            continue
        by_program.setdefault(index, []).append(proc)

    rows: list[InstalledMemory] = []
    for index, program in enumerate(programs):
        members = by_program.get(index, [])
        rss = sum(member.rss for member in members)
        pids = tuple(sorted(member.pid for member in members))
        cpus = [member.cpu_percent for member in members if member.cpu_percent is not None]
        rows.append(
            InstalledMemory(
                name=program.name,
                version=program.version,
                publisher=program.publisher,
                install_location=program.install_location,
                estimated_size_kb=program.estimated_size_kb,
                rss=rss,
                private_bytes=_sum_optional(members, "private_bytes"),
                private_working_set=_sum_optional(members, "private_working_set"),
                percent=(rss / total * 100) if total else 0.0,
                cpu_percent=sum(cpus) if cpus else None,
                process_count=len(members),
                pids=pids,
                lead_pid=pids[0] if pids else 0,
            )
        )
    return rows


def filter_installed(
    rows: Sequence[InstalledMemory],
    *,
    name: str | None,
    running_only: bool,
    min_bytes: int,
) -> list[InstalledMemory]:
    needle = (name or "").casefold()
    matched: list[InstalledMemory] = []
    for row in rows:
        if needle:
            haystack = " ".join(
                part
                for part in (row.name, row.publisher or "", row.version or "", row.install_location or "")
                if part
            ).casefold()
            if needle not in haystack:
                continue
        if running_only and row.process_count <= 0:
            continue
        if min_bytes > 0 and row.rss < min_bytes:
            continue
        matched.append(row)
    return matched


def is_broad_location(path: PureWindowsPath) -> bool:
    """True for roots that would swallow unrelated programs."""
    text = str(path).casefold().replace("/", "\\").rstrip("\\")
    if text.startswith("\\\\"):
        parts = [part for part in text.split("\\") if part]
        return len(parts) <= 2
    if len(text) <= 3:
        return True
    parts = [part for part in text.split("\\") if part]
    if len(parts) <= 1:
        return True
    leaf_pairs = {
        "windows",
        "users",
        "programdata",
        "program files",
        "program files (x86)",
    }
    if len(parts) == 2 and parts[1] in leaf_pairs:
        return True
    if len(parts) == 3 and parts[1] == "windows" and parts[2] in {"system32", "syswow64"}:
        return True
    return False


def is_under(child: PureWindowsPath, parent: PureWindowsPath) -> bool:
    if is_broad_location(parent):
        return False
    child_c = PureWindowsPath(str(child).casefold())
    parent_c = PureWindowsPath(str(parent).casefold())
    try:
        child_c.relative_to(parent_c)
    except ValueError:
        return False
    return True


def _location_path(location: str | None) -> PureWindowsPath | None:
    text = clean(location).strip('"')
    if not text:
        return None
    return PureWindowsPath(text)


def _exe_path(exe: str | None) -> PureWindowsPath | None:
    text = clean(exe).strip('"')
    if not text:
        return None
    return PureWindowsPath(text)


def _image_keys(proc: ProcessRow) -> set[str]:
    keys: set[str] = set()
    sources = [proc.name]
    exe = _exe_path(proc.exe)
    if exe is not None and exe.name:
        sources.append(exe.name)
    for source in sources:
        key = source.casefold()
        if not key:
            continue
        keys.add(key)
        if key.endswith(".exe"):
            keys.add(key[:-4])
        else:
            keys.add(key + ".exe")
    return keys


def _sum_optional(members: Sequence[ProcessRow], attr: str) -> int | None:
    if not members:
        return None
    values = [getattr(member, attr) for member in members]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_true(value: object) -> bool:
    if isinstance(value, str):
        return value.strip() in {"1", "true", "True"}
    return value is True or value == 1
