import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winmem.cli import main
from winmem.models import HostInfo, ProcessRow, Snapshot

ROOT = Path(__file__).resolve().parents[1]
HOST = HostInfo(
    hostname="desktop",
    os_label="Windows 11 Pro 23H2 (build 22631)",
    system="Windows",
    total=16 * 1024**3,
    available=8 * 1024**3,
    used=8 * 1024**3,
    percent=50.0,
    cpus=8,
)


def process(**kwargs) -> ProcessRow:
    values = dict(
        pid=1,
        name="helper.exe",
        username="luis",
        status="running",
        rss=1024 * 1024,
        private_bytes=None,
        private_working_set=None,
        private_rss=512 * 1024,
        percent=0.1,
        cpu_percent=None,
        exe=r"C:\Apps\helper.exe",
    )
    values.update(kwargs)
    return ProcessRow(**values)


class CliTests(unittest.TestCase):
    def test_help_mentions_task_manager(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(stdout):
                main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("Task Manager", stdout.getvalue())

    def test_rejects_bad_combinations(self):
        cases = [
            ["--top", "0"],
            ["--min-mb", "-1"],
            ["--watch", "0.1"],
            ["--json", "--csv"],
            ["--sort", "pid", "--group"],
            ["--installed", "--group"],
            ["--installed", "--user", "luis"],
            ["--watch", "1", "--json"],
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(SystemExit) as caught:
                    with redirect_stderr(io.StringIO()):
                        main(args)
                self.assertEqual(caught.exception.code, 2)

    def test_installed_requires_windows(self):
        if sys.platform == "win32":
            self.skipTest("registry listing is available on Windows")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["--installed"])
        self.assertEqual(code, 2)
        self.assertIn("Windows uninstall registry", stderr.getvalue())

    def test_group_min_memory_uses_the_sum(self):
        snapshot = Snapshot(
            host=HOST,
            rows=(
                process(pid=1, name="helper.exe", rss=1024 * 1024),
                process(pid=2, name="helper.exe", rss=1024 * 1024),
            ),
            skipped=0,
        )
        stdout = io.StringIO()
        with patch("winmem.cli.collect_snapshot", return_value=snapshot):
            with redirect_stdout(stdout):
                code = main(["--group", "--min-mb", "1.5", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["shown"], 1)
        self.assertEqual(payload["rows"][0]["rss_bytes"], 2 * 1024 * 1024)
        self.assertEqual(payload["rows"][0]["process_count"], 2)

        stdout = io.StringIO()
        with patch("winmem.cli.collect_snapshot", return_value=snapshot):
            with redirect_stdout(stdout):
                code = main(["--min-mb", "1.5", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["shown"], 0)

    def test_watch_stops_cleanly(self):
        snapshot = Snapshot(host=HOST, rows=(process(),), skipped=0)
        stdout = io.StringIO()
        with patch("winmem.cli.collect_snapshot", return_value=snapshot):
            with patch("winmem.cli.time.sleep", side_effect=KeyboardInterrupt):
                with redirect_stdout(stdout):
                    code = main(["--watch", "1", "--plain"])
        self.assertEqual(code, 130)
        self.assertIn("helper.exe", stdout.getvalue())

    def test_script_help_and_live_json(self):
        help_run = subprocess.run(
            [sys.executable, "list_programs.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_run.returncode, 0, help_run.stderr)
        self.assertIn("Private WS", help_run.stdout)

        module_run = subprocess.run(
            [sys.executable, "-m", "winmem", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(module_run.returncode, 0, module_run.stderr)

        live = subprocess.run(
            [sys.executable, "list_programs.py", "--no-cpu", "--top", "1", "--json", "--plain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(live.returncode, 0, live.stderr)
        payload = json.loads(live.stdout)
        self.assertGreater(payload["host"]["memory_total_bytes"], 0)
        self.assertEqual(payload["shown"], 1)
        self.assertIn("rss_bytes", payload["rows"][0])

        empty = subprocess.run(
            [
                sys.executable,
                "list_programs.py",
                "--no-cpu",
                "--name",
                "this-program-name-should-not-exist-zz",
                "--plain",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn("No running programs matched.", empty.stdout)


if __name__ == "__main__":
    unittest.main()
