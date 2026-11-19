"""Text, JSON, and CSV rendering for memory reports."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from winmem.collect import choose_private_attr
from winmem.models import HostInfo

SORT_LABELS = {
    "memory": "resident memory",
    "private": "private memory",
    "name": "name",
    "pid": "pid",
    "cpu": "cpu",
    "user": "user",
    "percent": "share of RAM",
    "count": "process count",
    "disk": "install size",
    "publisher": "publisher",
}


@dataclass(frozen=True)
class Style:
    plain: bool = False
    color: bool = False

    @property
    def missing(self) -> str:
        return "-" if self.plain else "—"

    @property
    def sep(self) -> str:
        return " | " if self.plain else "  ·  "


@dataclass(frozen=True)
class Column:
    header: str
    align: str
    value: Callable[[object], str]
    emphasize: bool = False


def format_bytes(value: int | None, *, missing: str = "—") -> str:
    """Format a byte count with binary units, the same scale Task Manager uses."""
    if value is None:
        return missing
    size = float(max(int(value), 0))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units[:-1]:
        if size < 1024:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {units[-1]}"


def format_percent(value: float | None, *, missing: str = "—") -> str:
    if value is None:
        return missing
    return f"{value:.1f}%"


def format_cpu(value: float | None, *, missing: str = "—") -> str:
    if value is None:
        return missing
    return f"{value:.1f}"


def memory_bar(percent: float, *, plain: bool, width: int = 28) -> str:
    clamped = min(100.0, max(0.0, percent))
    filled = int(round(width * clamped / 100))
    filled = min(width, max(0, filled))
    if plain:
        mark, empty = "#", "-"
    else:
        mark, empty = "█", "░"
    return "[" + (mark * filled) + (empty * (width - filled)) + "]"


def truncate(text: str, limit: int, *, plain: bool) -> str:
    text = " ".join(str(text).replace("\x00", " ").split())
    if len(text) <= limit:
        return text
    ellipsis = "..." if plain else "…"
    keep = limit - len(ellipsis)
    if keep < 1:
        return text[:limit]
    return text[:keep] + ellipsis


def render_process_report(
    host: HostInfo,
    rows: Sequence[object],
    *,
    grouped: bool,
    matched: int,
    matched_processes: int,
    matched_rss: int,
    matched_private_ws: int | None,
    matched_private_rss: int | None,
    skipped: int,
    sort_field: str,
    truncated: bool,
    verbose: bool,
    style: Style,
    refreshed_at: str | None = None,
) -> str:
    legend = _resident_legend(host, rows)
    lines = _header(host, style, legend, refreshed_at)
    columns = _process_columns(host, rows, grouped=grouped, verbose=verbose, style=style)
    if rows and columns:
        lines.extend(_table(columns, rows, style=style))
    else:
        lines.append("No running programs matched.")
    lines.append("")
    lines.extend(
        _process_footer(
            host,
            rows,
            grouped=grouped,
            matched=matched,
            matched_processes=matched_processes,
            matched_rss=matched_rss,
            matched_private_ws=matched_private_ws,
            matched_private_rss=matched_private_rss,
            skipped=skipped,
            sort_field=sort_field,
            truncated=truncated,
            style=style,
        )
    )
    return _finish(lines)


def render_installed_report(
    host: HostInfo,
    rows: Sequence[object],
    *,
    matched: int,
    running: int,
    unmatched_processes: int,
    matched_rss: int,
    matched_private_ws: int | None,
    skipped: int,
    sort_field: str,
    truncated: bool,
    verbose: bool,
    style: Style,
    refreshed_at: str | None = None,
) -> str:
    legend = (
        "Memory is the sum of processes running from that program's install folder. "
        "Install size is disk space recorded in the registry."
    )
    lines = _header(host, style, legend, refreshed_at)
    columns = _installed_columns(
        rows,
        windows=host.is_windows,
        verbose=verbose,
        style=style,
    )
    if rows and columns:
        lines.extend(_table(columns, rows, style=style))
    else:
        lines.append("No installed programs matched.")
    lines.append("")
    lines.extend(
        _installed_footer(
            host,
            matched=matched,
            running=running,
            unmatched_processes=unmatched_processes,
            matched_rss=matched_rss,
            matched_private_ws=matched_private_ws,
            skipped=skipped,
            sort_field=sort_field,
            truncated=truncated,
            style=style,
        )
    )
    return _finish(lines)


def process_json(
    host: HostInfo,
    rows: Sequence[object],
    *,
    grouped: bool,
    matched: int,
    skipped: int,
    sort_field: str,
) -> str:
    payload = {
        "host": _host_json(host),
        "view": "programs" if grouped else "processes",
        "sort": sort_field,
        "skipped_processes": skipped,
        "matched": matched,
        "shown": len(rows),
        "notes": _json_notes(host),
        "rows": [_group_json(row) if grouped else _process_json(row) for row in rows],
    }
    return json.dumps(payload, indent=2) + "\n"


def installed_json(
    host: HostInfo,
    rows: Sequence[object],
    *,
    matched: int,
    unmatched_processes: int,
    skipped: int,
    sort_field: str,
) -> str:
    payload = {
        "host": _host_json(host),
        "view": "installed",
        "sort": sort_field,
        "skipped_processes": skipped,
        "matched": matched,
        "shown": len(rows),
        "unmatched_processes": unmatched_processes,
        "notes": [
            "rss_bytes sums processes whose executable is inside the install folder.",
            "install_size_kb is the registry estimate of disk use, not memory.",
            *_json_notes(host),
        ],
        "rows": [_installed_json_row(row) for row in rows],
    }
    return json.dumps(payload, indent=2) + "\n"


def process_csv(rows: Sequence[object], *, grouped: bool) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if grouped:
        writer.writerow(
            [
                "name",
                "username",
                "process_count",
                "pids",
                "rss_bytes",
                "private_working_set_bytes",
                "private_bytes",
                "private_rss_bytes",
                "percent_ram",
                "cpu_percent",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.name,
                    row.username or "",
                    row.process_count,
                    ";".join(str(pid) for pid in row.pids),
                    row.rss,
                    _blank(row.private_working_set),
                    _blank(row.private_bytes),
                    _blank(row.private_rss),
                    _percent_csv(row.percent),
                    _blank_number(row.cpu_percent),
                ]
            )
    else:
        writer.writerow(
            [
                "pid",
                "name",
                "username",
                "status",
                "rss_bytes",
                "private_working_set_bytes",
                "private_bytes",
                "private_rss_bytes",
                "percent_ram",
                "cpu_percent",
                "exe",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.pid,
                    row.name,
                    row.username or "",
                    row.status or "",
                    row.rss,
                    _blank(row.private_working_set),
                    _blank(row.private_bytes),
                    _blank(row.private_rss),
                    _percent_csv(row.percent),
                    _blank_number(row.cpu_percent),
                    row.exe or "",
                ]
            )
    return buffer.getvalue()


def installed_csv(rows: Sequence[object]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "name",
            "version",
            "publisher",
            "install_location",
            "install_size_kb",
            "process_count",
            "pids",
            "rss_bytes",
            "private_working_set_bytes",
            "private_bytes",
            "percent_ram",
            "cpu_percent",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.name,
                row.version or "",
                row.publisher or "",
                row.install_location or "",
                _blank(row.estimated_size_kb),
                row.process_count,
                ";".join(str(pid) for pid in row.pids),
                row.rss,
                _blank(row.private_working_set),
                _blank(row.private_bytes),
                _percent_csv(row.percent),
                _blank_number(row.cpu_percent),
            ]
        )
    return buffer.getvalue()


def _process_columns(host: HostInfo, rows: Sequence[object], *, grouped: bool, verbose: bool, style: Style) -> list[Column]:
    resident = "Working set" if host.is_windows else "RSS"
    columns: list[Column] = []
    if not grouped:
        columns.append(Column("PID", "right", lambda row: str(row.pid)))
    columns.append(
        Column("Program", "left", lambda row: truncate(row.name, 36, plain=style.plain))
    )
    columns.append(
        Column(
            "User",
            "left",
            lambda row: truncate(row.username or style.missing, 22, plain=style.plain),
        )
    )
    if grouped:
        columns.append(Column("Processes", "right", lambda row: str(row.process_count)))
    columns.append(
        Column(
            resident,
            "right",
            lambda row: format_bytes(row.rss, missing=style.missing),
            emphasize=True,
        )
    )
    private_header = _private_header(rows)
    if private_header is not None:
        attr = choose_private_attr(rows)
        columns.append(
            Column(
                private_header,
                "right",
                lambda row, attr=attr: format_bytes(getattr(row, attr), missing=style.missing),
                emphasize=True,
            )
        )
    columns.append(Column("%RAM", "right", lambda row: format_percent(row.percent, missing=style.missing)))
    if _any(rows, "cpu_percent"):
        columns.append(Column("CPU%", "right", lambda row: format_cpu(row.cpu_percent, missing=style.missing)))
    if verbose and not grouped:
        columns.append(
            Column("Status", "left", lambda row: truncate(row.status or style.missing, 16, plain=style.plain))
        )
    if verbose and private_header != "Private bytes" and _any(rows, "private_bytes"):
        columns.append(
            Column(
                "Private bytes",
                "right",
                lambda row: format_bytes(row.private_bytes, missing=style.missing),
            )
        )
    if verbose and not grouped:
        columns.append(
            Column(
                "Executable",
                "left",
                lambda row: truncate(row.exe or style.missing, 48, plain=style.plain),
            )
        )
    return columns


def _installed_columns(
    rows: Sequence[object],
    *,
    windows: bool,
    verbose: bool,
    style: Style,
) -> list[Column]:
    columns = [
        Column("Program", "left", lambda row: truncate(row.name, 40, plain=style.plain)),
        Column("Version", "left", lambda row: truncate(row.version or style.missing, 16, plain=style.plain)),
        Column(
            "Publisher",
            "left",
            lambda row: truncate(row.publisher or style.missing, 24, plain=style.plain),
        ),
    ]
    if _any(rows, "estimated_size_kb"):
        columns.append(
            Column(
                "Install size",
                "right",
                lambda row: format_bytes(
                    None if row.estimated_size_kb is None else row.estimated_size_kb * 1024,
                    missing=style.missing,
                ),
            )
        )
    columns.append(Column("Processes", "right", lambda row: str(row.process_count)))
    resident = "Working set" if windows else "RSS"
    columns.append(
        Column(resident, "right", lambda row: format_bytes(row.rss, missing=style.missing), emphasize=True)
    )
    private_header = _private_header(rows)
    if private_header is not None:
        attr = choose_private_attr(rows)
        columns.append(
            Column(
                private_header,
                "right",
                lambda row, attr=attr: format_bytes(getattr(row, attr), missing=style.missing),
                emphasize=True,
            )
        )
    columns.append(Column("%RAM", "right", lambda row: format_percent(row.percent, missing=style.missing)))
    if _any(rows, "cpu_percent"):
        columns.append(Column("CPU%", "right", lambda row: format_cpu(row.cpu_percent, missing=style.missing)))
    if verbose:
        columns.append(
            Column(
                "Install folder",
                "left",
                lambda row: truncate(row.install_location or style.missing, 48, plain=style.plain),
            )
        )
    return columns


def _private_header(rows: Sequence[object]) -> str | None:
    if not rows:
        return None
    attr = choose_private_attr(rows)
    if not _any(rows, attr):
        return None
    return {
        "private_working_set": "Private WS",
        "private_rss": "Private RSS",
        "private_bytes": "Private bytes",
    }[attr]


def _table(columns: Sequence[Column], rows: Sequence[object], *, style: Style) -> list[str]:
    rendered = [[column.value(row) for column in columns] for row in rows]
    widths = [len(column.header) for column in columns]
    for cells in rendered:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))

    def paint(text: str, code: str | None) -> str:
        if not style.color or not code:
            return text
        return f"\033[{code}m{text}\033[0m"

    def format_row(values: Sequence[str], *, header: bool, emphasize: bool) -> str:
        parts: list[str] = []
        for index, value in enumerate(values):
            if columns[index].align == "right":
                text = value.rjust(widths[index])
            else:
                text = value.ljust(widths[index])
            if header:
                text = paint(text, "1")
            elif emphasize and columns[index].emphasize:
                text = paint(text, "33")
            parts.append(text)
        return "  ".join(parts)

    lines = [
        format_row([column.header for column in columns], header=True, emphasize=False),
        "  ".join("-" * width for width in widths),
    ]
    for row, cells in zip(rows, rendered):
        lines.append(
            format_row(cells, header=False, emphasize=getattr(row, "percent", 0) >= 10)
        )
    return lines


def _header(host: HostInfo, style: Style, legend: str, refreshed_at: str | None) -> list[str]:
    identity = [host.hostname, host.os_label]
    if host.cpus:
        identity.append(f"{host.cpus} CPUs")
    if refreshed_at:
        identity.append(refreshed_at)
    percent = format_percent(host.percent, missing=style.missing)
    if style.color and host.percent >= 90:
        percent = f"\033[31m{percent}\033[0m"
    elif style.color and host.percent >= 75:
        percent = f"\033[33m{percent}\033[0m"
    return [
        "Running programs",
        style.sep.join(identity),
        (
            f"RAM in use  {memory_bar(host.percent, plain=style.plain)}  "
            f"{format_bytes(host.used, missing=style.missing)} of "
            f"{format_bytes(host.total, missing=style.missing)} ({percent})"
            f"{style.sep}{format_bytes(host.available, missing=style.missing)} available"
        ),
        legend,
        "",
    ]


def _process_footer(
    host: HostInfo,
    rows: Sequence[object],
    *,
    grouped: bool,
    matched: int,
    matched_processes: int,
    matched_rss: int,
    matched_private_ws: int | None,
    matched_private_rss: int | None,
    skipped: int,
    sort_field: str,
    truncated: bool,
    style: Style,
) -> list[str]:
    sort_label = SORT_LABELS.get(sort_field, sort_field)
    if grouped:
        summary = f"{_count(matched, 'program')} ({_count(matched_processes, 'process')})"
        if truncated:
            head = f"Showing {len(rows)} of {summary}"
        else:
            head = summary
    elif truncated:
        head = f"Showing {len(rows)} of {_count(matched, 'process')}"
    else:
        head = _count(matched, "process")
    if skipped:
        head += f"{style.sep}{skipped} inaccessible"
    head += f"{style.sep}sorted by {sort_label}"
    lines = [head]
    if truncated:
        lines.append("Totals cover every match, including rows left out by --top.")
    if matched:
        amount = format_bytes(matched_rss, missing=style.missing)
        if host.is_windows:
            sentence = f"Working sets sum to {amount}."
        else:
            sentence = f"Resident memory sums to {amount}."
        lines.append(sentence + " Shared pages count toward every process that maps them.")
        lines.append(_system_gap_note())
        if matched_private_ws is not None:
            lines.append(
                f"Private working sets sum to {format_bytes(matched_private_ws, missing=style.missing)}. "
                "Task Manager uses that private working set as its Memory column."
            )
        elif matched_private_rss is not None:
            lines.append(
                f"Private RSS sums to {format_bytes(matched_private_rss, missing=style.missing)}. "
                "That is resident memory minus pages shared with other processes."
            )
    return lines


def _installed_footer(
    host: HostInfo,
    *,
    matched: int,
    running: int,
    unmatched_processes: int,
    matched_rss: int,
    matched_private_ws: int | None,
    skipped: int,
    sort_field: str,
    truncated: bool,
    style: Style,
) -> list[str]:
    sort_label = SORT_LABELS.get(sort_field, sort_field)
    summary = f"{_count(matched, 'installed program')} ({running} running)"
    if truncated:
        head = f"Showing the printed rows of {summary}"
    else:
        head = summary
    if skipped:
        head += f"{style.sep}{_count(skipped, 'process')} inaccessible"
    head += f"{style.sep}sorted by {sort_label}"
    lines = [head]
    if truncated:
        lines.append("Totals cover every match, including rows left out by --top.")
    if unmatched_processes:
        lines.append(
            f"{unmatched_processes} running processes are Windows components, services, "
            "or programs without an uninstall entry."
        )
    if matched_rss:
        amount = format_bytes(matched_rss, missing=style.missing)
        if host.is_windows:
            lines.append(f"Working sets of matched programs sum to {amount}.")
        else:
            lines.append(f"Resident memory of matched programs sums to {amount}.")
        if matched_private_ws is not None:
            lines.append(
                "Private working sets sum to "
                f"{format_bytes(matched_private_ws, missing=style.missing)}."
            )
        lines.append(_system_gap_note())
    return lines


def _system_gap_note() -> str:
    return (
        "Process totals do not add up to RAM in use. "
        "That figure also includes the kernel and memory the system is holding."
    )


def _resident_legend(host: HostInfo, rows: Sequence[object]) -> str:
    if host.is_windows and _any(rows, "private_working_set"):
        return "Working set includes shared pages. Private WS is the Task Manager Memory column."
    if _any(rows, "private_rss"):
        return "RSS includes shared pages. Private RSS is resident memory minus shared pages."
    if host.is_windows:
        return "Working set includes shared pages."
    return "RSS includes shared pages."


def _host_json(host: HostInfo) -> dict:
    return {
        "hostname": host.hostname,
        "os": host.os_label,
        "system": host.system,
        "cpus": host.cpus,
        "memory_total_bytes": host.total,
        "memory_available_bytes": host.available,
        "memory_used_bytes": host.used,
        "memory_percent": host.percent,
    }


def _process_json(row: object) -> dict:
    return {
        "pid": row.pid,
        "name": row.name,
        "username": row.username,
        "status": row.status,
        "rss_bytes": row.rss,
        "private_working_set_bytes": row.private_working_set,
        "private_bytes": row.private_bytes,
        "private_rss_bytes": row.private_rss,
        "percent_ram": row.percent,
        "cpu_percent": row.cpu_percent,
        "exe": row.exe,
    }


def _group_json(row: object) -> dict:
    return {
        "name": row.name,
        "username": row.username,
        "process_count": row.process_count,
        "pids": list(row.pids),
        "rss_bytes": row.rss,
        "private_working_set_bytes": row.private_working_set,
        "private_bytes": row.private_bytes,
        "private_rss_bytes": row.private_rss,
        "percent_ram": row.percent,
        "cpu_percent": row.cpu_percent,
    }


def _installed_json_row(row: object) -> dict:
    return {
        "name": row.name,
        "version": row.version,
        "publisher": row.publisher,
        "install_location": row.install_location,
        "install_size_kb": row.estimated_size_kb,
        "process_count": row.process_count,
        "pids": list(row.pids),
        "rss_bytes": row.rss,
        "private_working_set_bytes": row.private_working_set,
        "private_bytes": row.private_bytes,
        "percent_ram": row.percent,
        "cpu_percent": row.cpu_percent,
    }


def _json_notes(host: HostInfo) -> list[str]:
    if host.is_windows:
        return [
            "rss_bytes is the working set and includes shared pages.",
            "private_working_set_bytes is the Windows 11 Task Manager Memory column when present.",
            "private_bytes is the process commit charge.",
        ]
    return [
        "rss_bytes is resident set size and includes shared pages.",
        "private_rss_bytes is resident memory minus shared pages.",
    ]


def _count(value: int, singular: str) -> str:
    if singular == "process":
        plural = "processes"
    else:
        plural = singular + "s"
    word = singular if value == 1 else plural
    return f"{value} {word}"


def _any(rows: Sequence[object], attr: str) -> bool:
    return any(getattr(row, attr, None) is not None for row in rows)


def _blank(value: int | None) -> int | str:
    return "" if value is None else value


def _blank_number(value: float | None) -> float | str:
    return "" if value is None else round(value, 4)


def _percent_csv(value: float) -> float:
    return round(value, 4)


def _finish(lines: Sequence[str]) -> str:
    text = "\n".join(lines).rstrip() + "\n"
    return text
