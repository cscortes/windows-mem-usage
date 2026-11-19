"""Command-line interface for listing program memory."""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time

from winmem import __version__
from winmem.collect import (
    GROUP_SORTS,
    INSTALLED_SORTS,
    PROCESS_SORTS,
    CollectionError,
    MissingDependency,
    apply_min_memory,
    choose_private_attr,
    collect_snapshot,
    descending_by_default,
    filter_processes,
    group_rows,
    sort_rows,
    sum_attr,
)
from winmem.installed import (
    InstalledProgramsUnavailable,
    attach_process_memory,
    filter_installed,
    load_installed_programs,
)
from winmem.ledger import build_ledger, facts_from_snapshot
from winmem.render import (
    Style,
    installed_csv,
    installed_json,
    process_csv,
    process_json,
    render_installed_report,
    render_process_report,
)

_ALL_SORTS = tuple(sorted(set(PROCESS_SORTS) | set(GROUP_SORTS) | set(INSTALLED_SORTS)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Show where RAM in use goes, then list each program. "
            "On Windows 11, Task Manager's Memory column is private RAM, so adding "
            "it up is less than the in-use total. This report breaks that total "
            "into pieces that add back up, including kernel memory and memory "
            "not tied to one process. The same command works on Windows 10, "
            "Linux, and macOS."
        ),
        epilog=(
            "examples:\n"
            "  list_programs.py\n"
            "  list_programs.py --brief\n"
            "  list_programs.py --top 20\n"
            "  list_programs.py --group\n"
            "  list_programs.py --name chrome\n"
            "  list_programs.py --min-mb 100\n"
            "  list_programs.py --json\n"
            "  list_programs.py --csv > memory.csv\n"
            "  list_programs.py --watch 2\n"
            "  list_programs.py --installed\n"
            "  list_programs.py --installed --running-only\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--top", type=_positive_int, help="Show only the first N rows after sorting.")
    parser.add_argument("--name", help="Keep rows whose name, path, publisher, or version contains this text.")
    parser.add_argument("--user", help="Keep processes owned by a user name that contains this text.")
    parser.add_argument(
        "--min-mb",
        type=_nonnegative_float,
        default=0.0,
        help="Hide rows whose resident memory is below this many megabytes.",
    )
    parser.add_argument(
        "--group",
        action="store_true",
        help="Add up processes that share a program name.",
    )
    parser.add_argument(
        "--installed",
        action="store_true",
        help="List programs from the Windows uninstall registry and add up matching processes.",
    )
    parser.add_argument(
        "--running-only",
        action="store_true",
        help="Hide installed programs that are not running, and hide processes using no resident memory.",
    )
    parser.add_argument(
        "--sort",
        default=None,
        choices=_ALL_SORTS,
        help=(
            "Sort key. Memory, CPU, and size sorts start with the largest. Name sorts start at A. "
            "The default is private RAM when that figure is available."
        ),
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help="Show only the breakdown of RAM in use, not each process.",
    )
    order = parser.add_mutually_exclusive_group()
    order.add_argument("--asc", action="store_true", help="Sort from low to high, or A to Z for names.")
    order.add_argument("--desc", action="store_true", help="Sort from high to low, or Z to A for names.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Print JSON to standard output.")
    output.add_argument("--csv", action="store_true", help="Print CSV to standard output. Memory values are bytes.")
    parser.add_argument(
        "--watch",
        type=_watch_interval,
        help="Refresh the table. The number is the pause between refreshes, in seconds.",
    )
    parser.add_argument("--verbose", action="store_true", help="Add status, path, and commit-charge columns.")
    parser.add_argument("--no-cpu", action="store_true", help="Skip the short CPU sample so the report returns immediately.")
    parser.add_argument("--no-color", action="store_true", help="Do not colorize the table.")
    parser.add_argument("--plain", action="store_true", help="Use ASCII characters for bars, dashes, and separators.")
    parser.add_argument("--version", action="version", version=f"list-programs {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    validate(parser, args)
    try:
        if args.watch is not None:
            return _watch(args)
        emit(_render_once(args, refreshed_at=None))
        return 0
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0
    except InstalledProgramsUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (MissingDependency, CollectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.watch is not None and (args.json or args.csv):
        parser.error("--watch works with the table view; leave off --json and --csv")
    if args.installed and args.group:
        parser.error("use either --installed or --group")
    if args.installed and args.user:
        parser.error("--user filters running processes, so it cannot be combined with --installed")
    if args.installed:
        allowed = INSTALLED_SORTS
    elif args.group:
        allowed = GROUP_SORTS
    else:
        allowed = PROCESS_SORTS
    if args.sort is not None and args.sort not in allowed:
        names = ", ".join(allowed)
        parser.error(f"--sort {args.sort} is not available for this view (available: {names})")


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def _watch(args: argparse.Namespace) -> int:
    interactive = sys.stdout.isatty() and not args.plain
    first = True
    while True:
        if interactive:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        elif not first:
            sys.stdout.write("\n")
        first = False
        stamp = time.strftime("%H:%M:%S")
        emit(_render_once(args, refreshed_at=stamp))
        time.sleep(args.watch)


def _render_once(args: argparse.Namespace, refreshed_at: str | None) -> str:
    style = _style(args)
    if args.installed and sys.platform != "win32":
        raise InstalledProgramsUnavailable(platform.system() or sys.platform)
    snapshot = collect_snapshot(sample_cpu=not args.no_cpu)
    if args.installed:
        return _render_installed(args, snapshot, style, refreshed_at)
    return _render_processes(args, snapshot, style, refreshed_at)


def _render_processes(args: argparse.Namespace, snapshot, style: Style, refreshed_at: str | None) -> str:
    sort_field = _effective_sort(args, snapshot)
    ledger = build_ledger(facts_from_snapshot(snapshot))
    filtered = filter_processes(snapshot.rows, name=args.name, user=args.user)
    if args.group:
        working = group_rows(filtered, snapshot.host.total)
    else:
        working = list(filtered)
    if args.running_only:
        working = [row for row in working if row.rss > 0]
    working = apply_min_memory(working, _mb_to_bytes(args.min_mb))
    private_attr = choose_private_attr(working)
    ordered = sort_rows(
        working,
        sort_field,
        descending=_descending(args, sort_field),
        private_attr=private_attr,
    )
    visible = ordered[: args.top] if args.top else ordered
    matched_processes = sum(getattr(row, "process_count", 1) for row in working)
    if args.json:
        return process_json(
            snapshot.host,
            visible,
            grouped=args.group,
            matched=len(working),
            skipped=snapshot.skipped,
            sort_field=sort_field,
            ledger=ledger,
        )
    if args.csv:
        return process_csv(visible, grouped=args.group)
    narrowed = bool(args.name or args.user or args.min_mb or args.running_only or args.top)
    scope = "The breakdown above is the whole computer. The table below is the current filter." if narrowed else None
    return render_process_report(
        snapshot.host,
        visible,
        grouped=args.group,
        matched=len(working),
        matched_processes=matched_processes,
        matched_rss=sum(row.rss for row in working),
        matched_private_ws=sum_attr(working, "private_working_set"),
        matched_private_rss=sum_attr(working, "private_rss"),
        skipped=snapshot.skipped,
        sort_field=sort_field,
        truncated=bool(args.top) and len(visible) < len(working),
        verbose=args.verbose,
        style=style,
        refreshed_at=refreshed_at,
        ledger=ledger,
        brief=args.brief,
        scope_note=scope,
    )


def _render_installed(args: argparse.Namespace, snapshot, style: Style, refreshed_at: str | None) -> str:
    programs = load_installed_programs()
    attached = attach_process_memory(programs, snapshot.rows, snapshot.host.total)
    matched_pids = {pid for row in attached for pid in row.pids}
    unmatched = sum(1 for proc in snapshot.rows if proc.pid not in matched_pids)
    working = filter_installed(
        attached,
        name=args.name,
        running_only=args.running_only,
        min_bytes=_mb_to_bytes(args.min_mb),
    )
    sort_field = args.sort or "memory"
    private_attr = choose_private_attr(working)
    ordered = sort_rows(
        working,
        sort_field,
        descending=_descending(args, sort_field),
        private_attr=private_attr,
    )
    visible = ordered[: args.top] if args.top else ordered
    if args.json:
        return installed_json(
            snapshot.host,
            visible,
            matched=len(working),
            unmatched_processes=unmatched,
            skipped=snapshot.skipped,
            sort_field=sort_field,
        )
    if args.csv:
        return installed_csv(visible)
    running = sum(1 for row in working if row.process_count)
    return render_installed_report(
        snapshot.host,
        visible,
        matched=len(working),
        running=running,
        unmatched_processes=unmatched,
        matched_rss=sum(row.rss for row in working),
        matched_private_ws=sum_attr(working, "private_working_set"),
        skipped=snapshot.skipped,
        sort_field=sort_field,
        truncated=bool(args.top) and len(visible) < len(working),
        verbose=args.verbose,
        style=style,
        refreshed_at=refreshed_at,
    )


def _style(args: argparse.Namespace) -> Style:
    plain = args.plain or not _supports_unicode(sys.stdout)
    color = (
        not plain
        and not args.no_color
        and not args.json
        and not args.csv
        and not os.environ.get("NO_COLOR")
        and sys.stdout.isatty()
    )
    return Style(plain=plain, color=color)


def _supports_unicode(stream) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "█░—…·".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _effective_sort(args: argparse.Namespace, snapshot) -> str:
    if args.sort:
        return args.sort
    if snapshot.host.is_windows and any(row.private_working_set is not None for row in snapshot.rows):
        return "private"
    if any(row.private_rss is not None for row in snapshot.rows):
        return "private"
    return "memory"


def _descending(args: argparse.Namespace, sort_field: str | None = None) -> bool:
    if args.asc:
        return False
    if args.desc:
        return True
    return descending_by_default(sort_field or args.sort or "memory")


def _mb_to_bytes(value: float) -> int:
    return int(value * 1024 * 1024)


def emit(text: str) -> None:
    if text and not text.endswith("\n"):
        text += "\n"
    sys.stdout.write(text)
    sys.stdout.flush()


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _nonnegative_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be zero or greater") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return value


def _watch_interval(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be at least 0.25 seconds") from exc
    if value < 0.25:
        raise argparse.ArgumentTypeError("must be at least 0.25 seconds")
    return value
