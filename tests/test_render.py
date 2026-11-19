import csv
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winmem.models import GroupRow, HostInfo, InstalledMemory, ProcessRow
from winmem.render import (
    Style,
    installed_csv,
    process_csv,
    process_json,
    render_installed_report,
    render_process_report,
)


WINDOWS = HostInfo(
    hostname="desktop",
    os_label="Windows 11 Pro 23H2 (build 22631)",
    system="Windows",
    total=16 * 1024**3,
    available=6 * 1024**3,
    used=10 * 1024**3,
    percent=62.5,
    cpus=8,
)
LINUX = HostInfo(
    hostname="build",
    os_label="Linux 6.8.0",
    system="Linux",
    total=8 * 1024**3,
    available=2 * 1024**3,
    used=6 * 1024**3,
    percent=75.0,
    cpus=4,
)


def process(**kwargs) -> ProcessRow:
    values = dict(
        pid=10,
        name="chrome.exe",
        username="luis",
        status="running",
        rss=2 * 1024**3,
        private_bytes=3 * 1024**3,
        private_working_set=1024**3,
        private_rss=None,
        percent=12.5,
        cpu_percent=4.5,
        exe=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    values.update(kwargs)
    return ProcessRow(**values)


class RenderTests(unittest.TestCase):
    def test_windows_table_names_task_manager_memory(self):
        report = render_process_report(
            WINDOWS,
            [process()],
            grouped=False,
            matched=1,
            matched_processes=1,
            matched_rss=2 * 1024**3,
            matched_private_ws=1024**3,
            matched_private_rss=None,
            skipped=2,
            sort_field="memory",
            truncated=False,
            verbose=True,
            style=Style(plain=True, color=False),
        )
        self.assertIn("Running programs", report)
        self.assertIn("Windows 11 Pro 23H2", report)
        self.assertIn("RAM in use", report)
        self.assertIn("Private WS is the Task Manager Memory column.", report)
        self.assertIn("Working set", report)
        self.assertIn("Private WS", report)
        self.assertIn("chrome.exe", report)
        self.assertIn("1 process", report)
        self.assertIn("2 inaccessible", report)
        self.assertIn("Private working sets sum to 1.0 GB.", report)
        self.assertNotIn("\033", report)
        self.assertIn("Executable", report)

    def test_running_total_adds_private_ram_in_the_order_shown(self):
        rows = [
            process(pid=1, name="chrome.exe", private_working_set=1024**3),
            process(pid=2, name="explorer.exe", private_working_set=512 * 1024**2, percent=4.0),
            process(pid=3, name="hidden.exe", private_working_set=None, percent=1.0),
        ]
        report = render_process_report(
            WINDOWS,
            rows,
            grouped=False,
            matched=3,
            matched_processes=3,
            matched_rss=sum(row.rss for row in rows),
            matched_private_ws=1024**3 + 512 * 1024**2,
            matched_private_rss=None,
            skipped=0,
            sort_field="private",
            truncated=False,
            verbose=False,
            style=Style(plain=True),
        )
        header = next(line for line in report.splitlines() if "Private WS" in line and "Running total" in line)
        self.assertLess(header.index("Private WS"), header.index("Running total"))
        self.assertLess(header.index("Running total"), header.index("Working set"))
        self.assertIn("1.5 GB", report)
        self.assertIn(
            "Running total of private RAM after the last process is 1.5 GB. RAM in use is 10.0 GB.",
            report,
        )
        text = process_csv(rows, grouped=False)
        parsed = list(csv.reader(io.StringIO(text)))
        self.assertEqual(parsed[1][-1], str(1024**3))
        self.assertEqual(parsed[2][-1], str(1024**3 + 512 * 1024**2))
        self.assertEqual(parsed[3][-1], parsed[2][-1])
        payload = json.loads(
            process_json(WINDOWS, rows, grouped=False, matched=3, skipped=0, sort_field="private")
        )
        self.assertEqual(payload["running_total_of"], "private_working_set")
        self.assertEqual(payload["rows"][2]["running_total_bytes"], 1024**3 + 512 * 1024**2)

    def test_linux_table_uses_rss(self):
        row = process(
            name="python",
            private_working_set=None,
            private_bytes=None,
            private_rss=100 * 1024**2,
            cpu_percent=None,
            percent=1.0,
            rss=200 * 1024**2,
        )
        report = render_process_report(
            LINUX,
            [row],
            grouped=False,
            matched=1,
            matched_processes=1,
            matched_rss=row.rss,
            matched_private_ws=None,
            matched_private_rss=row.private_rss,
            skipped=0,
            sort_field="memory",
            truncated=False,
            verbose=False,
            style=Style(plain=False, color=False),
        )
        self.assertIn("RSS", report)
        self.assertIn("Private RSS", report)
        self.assertNotIn("CPU%", report)
        self.assertNotIn("Working set", report)
        self.assertIn("Resident memory sums to", report)

    def test_color_marks_a_large_process(self):
        report = render_process_report(
            WINDOWS,
            [process(percent=15)],
            grouped=False,
            matched=1,
            matched_processes=1,
            matched_rss=1,
            matched_private_ws=1,
            matched_private_rss=None,
            skipped=0,
            sort_field="memory",
            truncated=False,
            verbose=False,
            style=Style(plain=False, color=True),
        )
        self.assertIn("\033[33m", report)

    def test_grouped_footer_and_json(self):
        grouped = GroupRow(
            name="chrome.exe",
            username="luis",
            process_count=3,
            rss=300,
            private_bytes=None,
            private_working_set=200,
            private_rss=None,
            percent=1.0,
            cpu_percent=2.0,
            pids=(1, 2, 4),
            lead_pid=1,
        )
        report = render_process_report(
            WINDOWS,
            [grouped],
            grouped=True,
            matched=1,
            matched_processes=3,
            matched_rss=300,
            matched_private_ws=200,
            matched_private_rss=None,
            skipped=0,
            sort_field="memory",
            truncated=True,
            verbose=False,
            style=Style(plain=True),
        )
        self.assertIn("Showing 1 of 1 program (3 processes)", report)
        self.assertIn("Totals cover every match", report)
        payload = json.loads(
            process_json(
                WINDOWS,
                [grouped],
                grouped=True,
                matched=4,
                skipped=0,
                sort_field="memory",
            )
        )
        self.assertEqual(payload["view"], "programs")
        self.assertEqual(payload["rows"][0]["pids"], [1, 2, 4])
        self.assertEqual(payload["rows"][0]["process_count"], 3)
        self.assertIn("Task Manager", " ".join(payload["notes"]))

    def test_csv_schema(self):
        text = process_csv([process()], grouped=False)
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(
            rows[0],
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
            ],
        )
        self.assertEqual(rows[1][0], "10")
        self.assertEqual(rows[1][1], "chrome.exe")

    def test_installed_report(self):
        row = InstalledMemory(
            name="Google Chrome",
            version="131.0",
            publisher="Google LLC",
            install_location=r"C:\Program Files\Google\Chrome",
            estimated_size_kb=350_000,
            rss=1024**3,
            private_bytes=None,
            private_working_set=800 * 1024**2,
            percent=6.0,
            cpu_percent=None,
            process_count=4,
            pids=(1, 2, 3, 4),
            lead_pid=1,
        )
        report = render_installed_report(
            WINDOWS,
            [row],
            matched=1,
            running=1,
            unmatched_processes=12,
            matched_rss=row.rss,
            matched_private_ws=row.private_working_set,
            skipped=0,
            sort_field="memory",
            truncated=False,
            verbose=True,
            style=Style(plain=True),
        )
        self.assertIn("Google Chrome", report)
        self.assertIn("Install size", report)
        self.assertIn("1 installed program (1 running)", report)
        self.assertIn("12 running processes are Windows components", report)
        csv_rows = list(csv.reader(io.StringIO(installed_csv([row]))))
        self.assertEqual(csv_rows[1][0], "Google Chrome")
        self.assertEqual(csv_rows[1][4], "350000")

    def test_empty_report(self):
        report = render_process_report(
            WINDOWS,
            [],
            grouped=False,
            matched=0,
            matched_processes=0,
            matched_rss=0,
            matched_private_ws=None,
            matched_private_rss=None,
            skipped=0,
            sort_field="name",
            truncated=False,
            verbose=False,
            style=Style(plain=True),
        )
        self.assertIn("No running programs matched.", report)
        self.assertIn("sorted by name", report)


if __name__ == "__main__":
    unittest.main()
