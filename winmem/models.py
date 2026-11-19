"""Shared values for process memory snapshots."""

from __future__ import annotations

import platform
import socket
from dataclasses import dataclass


def clean(text: object | None) -> str:
    """Collapse whitespace and drop NUL bytes."""
    if text is None:
        return ""
    return " ".join(str(text).replace("\x00", " ").split())


def parse_build(version: str) -> int | None:
    """Return the Windows build from a version string such as ``10.0.22631``."""
    parts = (version or "").split(".")
    if len(parts) >= 3 and parts[2].isdigit():
        return int(parts[2])
    return None


def windows_label(
    release: str,
    version: str,
    product_name: str | None = None,
    display_version: str | None = None,
) -> str:
    """Describe a Windows install.

    Windows 11 still reports version 10.0 internally. Builds 22000 and newer
    are Windows 11 even when the registry product name still says Windows 10.
    """
    build = parse_build(version or "")
    product = clean(product_name)
    display = clean(display_version)
    server = "server" in product.casefold()

    if server and product:
        base = product
    elif build is not None and build >= 22000:
        if product.casefold().startswith("windows 10"):
            base = "Windows 11" + product[len("windows 10") :]
        elif product.casefold().startswith("windows 11"):
            base = product
        else:
            base = "Windows 11"
    elif build is not None and build >= 10240:
        if product.casefold().startswith("windows 10"):
            base = product
        else:
            base = "Windows 10"
    elif product:
        base = product
    elif release:
        base = f"Windows {release}"
    else:
        base = "Windows"

    if display and display.casefold() not in base.casefold():
        base = f"{base} {display}"
    if build is not None:
        return f"{base} (build {build})"
    return base


@dataclass(frozen=True)
class HostInfo:
    hostname: str
    os_label: str
    system: str
    total: int
    available: int
    used: int
    percent: float
    cpus: int | None = None

    @property
    def is_windows(self) -> bool:
        return self.system == "Windows"


def detect_host(
    total: int,
    available: int,
    *,
    cpus: int | None = None,
    product_name: str | None = None,
    display_version: str | None = None,
) -> HostInfo:
    """Build host memory info from the machine this process is running on."""
    system = platform.system() or "Unknown"
    if system == "Windows":
        label = windows_label(
            platform.release(),
            platform.version(),
            product_name,
            display_version,
        )
    else:
        label = f"{system} {platform.release()}".strip()
    total = int(total)
    available = int(available)
    used = max(total - available, 0)
    percent = (used / total * 100) if total else 0.0
    return HostInfo(
        hostname=socket.gethostname(),
        os_label=label,
        system=system,
        total=total,
        available=available,
        used=used,
        percent=percent,
        cpus=cpus,
    )


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    name: str
    username: str | None
    status: str | None
    rss: int
    private_bytes: int | None
    private_working_set: int | None
    private_rss: int | None
    percent: float
    cpu_percent: float | None
    exe: str | None


@dataclass(frozen=True)
class GroupRow:
    name: str
    username: str | None
    process_count: int
    rss: int
    private_bytes: int | None
    private_working_set: int | None
    private_rss: int | None
    percent: float
    cpu_percent: float | None
    pids: tuple[int, ...]
    lead_pid: int


@dataclass(frozen=True)
class InstalledProgram:
    name: str
    version: str | None
    publisher: str | None
    install_location: str | None
    icon_name: str | None
    estimated_size_kb: int | None


@dataclass(frozen=True)
class InstalledMemory:
    name: str
    version: str | None
    publisher: str | None
    install_location: str | None
    estimated_size_kb: int | None
    rss: int
    private_bytes: int | None
    private_working_set: int | None
    percent: float
    cpu_percent: float | None
    process_count: int
    pids: tuple[int, ...]
    lead_pid: int


@dataclass(frozen=True)
class Snapshot:
    host: HostInfo
    rows: tuple[ProcessRow, ...]
    skipped: int
