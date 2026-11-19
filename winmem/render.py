"""Text, JSON, and CSV rendering for memory reports."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from winmem.collect import choose_private_attr
from winmem.ledger import Ledger
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
    ledger: Ledger | None = None,
    brief: bool = False,
    scope_note: str | None = None,
) -> str:
    legend = "" if ledger is not None else _resident_legend(host, rows)
    lines = _header(host, style, legend, refreshed_at, titled=ledger is not None)
    if ledger is not None:
        lines.extend(render_ledger(ledger, style))
        lines.append("")
    if not brief:
        if scope_note:
            lines.append(scope_note)
            lines.append("")
        columns = _process_columns(
            host,
            rows,
            grouped=grouped,
            verbose=verbose,
            style=style,
            sort_field=sort_field,
        )
        if rows and columns:
            if ledger is not None:
                lines.append("Programs" if grouped else "Processes")
                lines.append("")
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
                with_ledger=ledger is not None,
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
    ledger: Ledger | None = None,
) -> str:
    payload = {
        "host": _host_json(host),
        "view": "programs" if grouped else "processes",
        "sort": sort_field,
        "skipped_processes": skipped,
        "matched": matched,
        "shown": len(rows),
        "notes": _json_notes(host),
        "rows": _rows_with_running_total(rows, grouped=grouped),
    }
    if rows:
        payload["running_total_of"] = usage_attr(rows)
    if ledger is not None:
        payload["ledger"] = _ledger_json(ledger)
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
                "running_total_bytes",
            ]
        )
        for row, total in zip(rows, running_totals(rows, usage_attr(rows))):
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
                    total,
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
                "running_total_bytes",
            ]
        )
        for row, total in zip(rows, running_totals(rows, usage_attr(rows))):
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
                    total,
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


def _process_columns(
    host: HostInfo,
    rows: Sequence[object],
    *,
    grouped: bool,
    verbose: bool,
    style: Style,
    sort_field: str = "memory",
) -> list[Column]:
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
    resident_column = Column(
        resident,
        "right",
        lambda row: format_bytes(row.rss, missing=style.missing),
        emphasize=True,
    )
    private_header = _private_header(rows)
    private_column = None
    if private_header is not None:
        attr = choose_private_attr(rows)
        private_column = Column(
            private_header,
            "right",
            lambda row, attr=attr: format_bytes(getattr(row, attr), missing=style.missing),
            emphasize=True,
        )
    if sort_field == "private" and private_column is not None:
        columns.extend((private_column, resident_column))
    else:
        columns.append(resident_column)
        if private_column is not None:
            columns.append(private_column)
    if rows:
        summed = _usage_header(rows, resident)
        _insert_after(columns, summed, _running_total_column(rows, style))
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


def render_ledger(ledger: Ledger, style: Style) -> list[str]:
    """Explain why a process list does not add up to RAM in use, then show the sum."""
    lines = list(ledger_intro(ledger, style))
    if any(line.key == "process_private" for line in ledger.in_use_lines):
        if ledger.system == "Windows":
            lines.append("The first line is the Memory column added up. The lines under it are in-use RAM that column does not show.")
        else:
            lines.append("The first line is the private part of every process, added up. The lines under it are the rest of the RAM in use.")
    lines.append("")
    lines.append(f"Where the {format_bytes(ledger.in_use, missing=style.missing)} in use goes")
    lines.extend(_ledger_block(ledger.in_use_lines, "In use", ledger.in_use, style))
    lines.append("These lines add up to RAM in use.")
    lines.append("")
    available = format_bytes(ledger.available, missing=style.missing)
    split = [line for line in ledger.available_lines if line.key != "available"]
    if not split:
        lines.append(f"The other {available} is available.")
    else:
        lines.append(f"The other {available} is available")
        if any(line.key == "standby" for line in ledger.available_lines):
            lines.append("Standby cache can be given to a program immediately. It is available, not in use.")
        lines.extend(_ledger_block(ledger.available_lines, "Available", ledger.available, style))
    lines.extend(ledger_followup(ledger, style))
    return lines


def ledger_intro(ledger: Ledger, style: Style) -> list[str]:
    used = format_bytes(ledger.in_use, missing=style.missing)
    total = format_bytes(ledger.total, missing=style.missing)
    if ledger.system == "Windows":
        return [
            f"Windows reports {used} in use out of {total}.",
            "The Memory column is each process's private RAM. Shared DLLs, the kernel, and pages",
            "not assigned to one process are inside that in-use total and absent from every row,",
            "so the column adds up short. Adding working sets instead counts each shared DLL",
            "again for every process, so that sum runs high.",
            "Compressed RAM is often a process named Memory Compression. Task Manager sometimes",
            "nests it under System, which makes it easy to miss.",
        ]
    return [
        f"This computer reports {used} in use out of {total}.",
        "Each process row is that process's own resident memory. Shared pages are left out",
        "or counted once per process, so adding the rows does not produce the in-use figure.",
        "The lines below are the whole computer, and they add up to it.",
    ]


def ledger_followup(ledger: Ledger, style: Style) -> list[str]:
    missing = style.missing
    lines: list[str] = []
    if ledger.working_set > 0:
        shown = format_bytes(ledger.working_set, missing=missing)
        if ledger.working_set > ledger.in_use:
            lines.append(
                f"Adding every working set gives {shown}. "
                "That is higher than RAM in use because shared memory is counted once for each process that maps it."
            )
        else:
            lines.append(
                f"Adding every working set gives {shown}. "
                "That is still not RAM in use, because shared pages and kernel memory are not one process row."
            )
    shown = next((line.amount for line in ledger.in_use_lines if line.key == "process_private"), None)
    if shown is not None and ledger.process_private_measured > shown:
        lines.append(
            f"Process private RAM measured {format_bytes(ledger.process_private_measured, missing=missing)}, "
            f"above RAM in use. The line uses {format_bytes(shown, missing=missing)} so the lines can add up."
        )
    if ledger.system == "Windows" and ledger.commit is not None:
        if ledger.commit_limit:
            promised = (
                f"Commit charge is {format_bytes(ledger.commit, missing=missing)} "
                f"of {format_bytes(ledger.commit_limit, missing=missing)}."
            )
        else:
            promised = f"Commit charge is {format_bytes(ledger.commit, missing=missing)}."
        lines.append(promised + " That is memory Windows has promised, including the page file. It is not RAM in use.")
    if ledger.hardware_reserved:
        lines.append(
            f"Hardware reserved is {format_bytes(ledger.hardware_reserved, missing=missing)}, "
            f"outside the {format_bytes(ledger.total, missing=missing)} the operating system is managing."
        )
    if ledger.skipped_processes:
        noun = "process" if ledger.skipped_processes == 1 else "processes"
        lines.append(
            f"{ledger.skipped_processes} {noun} could not be queried. "
            "RAM they hold is inside Other memory in use."
        )
    if ledger.omitted:
        lines.append(
            "These measurements overlap RAM already listed, so they are not separate lines: "
            + "; ".join(ledger.omitted)
            + "."
        )
    if ledger.available_was_scaled:
        lines.append("Standby and free measured larger than available, so those lines are fitted to the available total.")
    return lines


def _ledger_block(entries: Sequence[object], total_label: str, total: int, style: Style) -> list[str]:
    rows = [(entry.label, entry.amount) for entry in entries]
    rows.append((total_label, total))
    label_width = max((len(label) for label, _amount in rows), default=len(total_label))
    amount_width = max(len(format_bytes(amount, missing=style.missing)) for _label, amount in rows)
    body = []
    for label, amount in rows[:-1]:
        body.append(_ledger_row(label, amount, label_width, amount_width, style, bold=False))
    if rows[:-1]:
        body.append("  " + "-" * (label_width + 2 + amount_width))
    body.append(_ledger_row(total_label, total, label_width, amount_width, style, bold=True))
    return body


def _ledger_row(label: str, amount: int, label_width: int, amount_width: int, style: Style, *, bold: bool) -> str:
    text = f"  {label.ljust(label_width)}  {format_bytes(amount, missing=style.missing).rjust(amount_width)}"
    if bold and style.color:
        return f"\033[1m{text}\033[0m"
    return text


def _ledger_json(ledger: Ledger) -> dict:
    return {
        "total_bytes": ledger.total,
        "in_use_bytes": ledger.in_use,
        "available_bytes": ledger.available,
        "closes": ledger.closes(),
        "in_use": [{"key": line.key, "label": line.label, "bytes": line.amount} for line in ledger.in_use_lines],
        "available": [
            {"key": line.key, "label": line.label, "bytes": line.amount} for line in ledger.available_lines
        ],
        "working_set_bytes": ledger.working_set,
        "process_private_bytes": ledger.process_private_measured,
        "commit_bytes": ledger.commit,
        "commit_limit_bytes": ledger.commit_limit,
        "hardware_reserved_bytes": ledger.hardware_reserved,
        "notes": ledger_intro(ledger, Style(plain=True)) + ledger_followup(ledger, Style(plain=True)),
    }


def _header(
    host: HostInfo,
    style: Style,
    legend: str,
    refreshed_at: str | None,
    titled: bool = False,
) -> list[str]:
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
    lines = [
        "Where the RAM goes" if titled else "Running programs",
        style.sep.join(identity),
        (
            f"RAM in use  {memory_bar(host.percent, plain=style.plain)}  "
            f"{format_bytes(host.used, missing=style.missing)} of "
            f"{format_bytes(host.total, missing=style.missing)} ({percent})"
            f"{style.sep}{format_bytes(host.available, missing=style.missing)} available"
        ),
    ]
    if legend:
        lines.append(legend)
    lines.append("")
    return lines


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
    with_ledger: bool = False,
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
    running = _running_total_note(host, rows, grouped=grouped, truncated=truncated, style=style)
    if running:
        lines.append(running)
    if with_ledger:
        return lines
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


def usage_attr(rows: Sequence[object]) -> str:
    """The memory column a running total adds up.

    On Windows that is the private working set, which is Task Manager's Memory
    column. Elsewhere it is private resident memory when we have it.
    """
    if _any(rows, "private_working_set"):
        return "private_working_set"
    if _any(rows, "private_rss"):
        return "private_rss"
    return "rss"


def running_totals(rows: Sequence[object], attr: str) -> list[int]:
    """Cumulative memory, in the order the rows are shown. A blank cell adds nothing."""
    total = 0
    totals: list[int] = []
    for row in rows:
        value = getattr(row, attr, None)
        if value is not None:
            total += max(int(value), 0)
        totals.append(total)
    return totals


def _usage_header(rows: Sequence[object], resident: str) -> str:
    attr = usage_attr(rows)
    if attr == "rss":
        return resident
    return _private_header(rows) or resident


def _running_total_column(rows: Sequence[object], style: Style) -> Column:
    totals = {id(row): amount for row, amount in zip(rows, running_totals(rows, usage_attr(rows)))}
    return Column(
        "Running total",
        "right",
        lambda row, totals=totals: format_bytes(totals[id(row)], missing=style.missing),
        emphasize=True,
    )


def _insert_after(columns: list[Column], header: str, column: Column) -> None:
    for index, existing in enumerate(columns):
        if existing.header == header:
            columns.insert(index + 1, column)
            return
    columns.append(column)


def _running_total_note(
    host: HostInfo,
    rows: Sequence[object],
    *,
    grouped: bool,
    truncated: bool,
    style: Style,
) -> str | None:
    if not rows:
        return None
    attr = usage_attr(rows)
    total = running_totals(rows, attr)[-1]
    shown = format_bytes(total, missing=style.missing)
    in_use = format_bytes(host.used, missing=style.missing)
    label = {
        "private_working_set": "private RAM",
        "private_rss": "private RAM",
        "rss": "resident memory",
    }[attr]
    if truncated:
        return f"Running total of {label} for the rows shown is {shown}. RAM in use is {in_use}."
    noun = "program" if grouped else "process"
    return f"Running total of {label} after the last {noun} is {shown}. RAM in use is {in_use}."


def _rows_with_running_total(rows: Sequence[object], *, grouped: bool) -> list[dict]:
    if not rows:
        return []
    totals = running_totals(rows, usage_attr(rows))
    encoded = []
    for row, total in zip(rows, totals):
        item = _group_json(row) if grouped else _process_json(row)
        item["running_total_bytes"] = total
        encoded.append(item)
    return encoded


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
