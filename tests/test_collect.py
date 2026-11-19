import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winmem.collect import (
    apply_min_memory,
    build_snapshot,
    choose_private_attr,
    collect_snapshot,
    filter_processes,
    group_rows,
    sort_rows,
)
from winmem.models import HostInfo, ProcessRow


HOST = HostInfo(
    hostname="desktop",
    os_label="Windows 11 Pro 23H2 (build 22631)",
    system="Windows",
    total=16 * 1024**3,
    available=6 * 1024**3,
    used=10 * 1024**3,
    percent=62.5,
    cpus=8,
)


class Mem:
    def __init__(self, rss, shared=None, private=None):
        self.rss = rss
        self.vms = rss * 2
        if shared is not None:
            self.shared = shared
        if private is not None:
            self.private = private


class FakeProc:
    def __init__(self, pid, name, rss, user="alice", shared=None, private=None, exe=None, status="running", cpu=1.25):
        self.pid = pid
        self._name = name
        self._mem = Mem(rss, shared, private)
        self._user = user
        self._exe = exe
        self._status = status
        self._cpu = cpu

    def name(self):
        return self._name

    def memory_info(self):
        return self._mem

    def username(self):
        return self._user

    def status(self):
        return self._status

    def exe(self):
        return self._exe

    def cpu_percent(self, interval=None):
        return self._cpu


class Denied:
    pid = 42

    def memory_info(self):
        raise PermissionError("denied")


class Partial:
    pid = 7

    def memory_info(self):
        return Mem(rss=2048, shared=1024, private=4096)

    def name(self):
        return "widget.exe"

    def username(self):
        raise PermissionError("no user")

    def status(self):
        raise PermissionError("no status")

    def exe(self):
        raise PermissionError("no path")

    def cpu_percent(self, interval=None):
        raise PermissionError("no cpu")


def row(**kwargs) -> ProcessRow:
    values = dict(
        pid=1,
        name="chrome.exe",
        username="luis",
        status="running",
        rss=500,
        private_bytes=None,
        private_working_set=None,
        private_rss=None,
        percent=1.0,
        cpu_percent=None,
        exe=None,
    )
    values.update(kwargs)
    return ProcessRow(**values)


class CollectTests(unittest.TestCase):
    def test_snapshot_reads_memory_and_skips_failures(self):
        snapshot = build_snapshot(
            [
                FakeProc(1, "chrome.exe", 5_000, private=9_000, exe=r"C:\Chrome\chrome.exe"),
                Denied(),
                FakeProc(2, "", 100),
            ],
            total=50_000,
            available=20_000,
            private_ws={1: 4_000},
            sample_cpu=True,
            host=HOST,
        )
        self.assertEqual(snapshot.skipped, 1)
        self.assertEqual(len(snapshot.rows), 2)
        chrome = snapshot.rows[0]
        self.assertEqual(chrome.pid, 1)
        self.assertEqual(chrome.rss, 5_000)
        self.assertEqual(chrome.private_bytes, 9_000)
        self.assertEqual(chrome.private_working_set, 4_000)
        self.assertEqual(chrome.percent, 10.0)
        self.assertEqual(chrome.cpu_percent, 1.25)
        self.assertEqual(snapshot.rows[1].name, "pid 2")

    def test_private_rss_subtracts_shared_pages(self):
        snapshot = build_snapshot(
            [FakeProc(3, "python", 1_000, shared=250)],
            total=10_000,
            available=5_000,
            sample_cpu=False,
            host=HOST,
        )
        self.assertEqual(snapshot.rows[0].private_rss, 750)
        self.assertIsNone(snapshot.rows[0].cpu_percent)
        self.assertIsNone(snapshot.rows[0].private_working_set)

    def test_denied_fields_keep_the_process(self):
        snapshot = build_snapshot(
            [Partial()],
            total=10_000,
            available=5_000,
            sample_cpu=True,
            host=HOST,
        )
        proc = snapshot.rows[0]
        self.assertEqual(proc.name, "widget.exe")
        self.assertIsNone(proc.username)
        self.assertIsNone(proc.status)
        self.assertIsNone(proc.exe)
        self.assertIsNone(proc.cpu_percent)
        self.assertEqual(proc.private_rss, 1024)
        self.assertEqual(snapshot.skipped, 0)

    def test_filter_matches_name_and_executable(self):
        rows = [
            row(pid=1, name="chrome.exe", exe=r"C:\Chrome\chrome.exe"),
            row(pid=2, name="notepad.exe", exe=r"C:\Windows\notepad.exe", username="bob"),
        ]
        self.assertEqual([item.pid for item in filter_processes(rows, name="CHROME", user=None)], [1])
        self.assertEqual(
            [item.pid for item in filter_processes(rows, name="windows", user=None)],
            [2],
        )
        self.assertEqual([item.pid for item in filter_processes(rows, name=None, user="bob")], [2])

    def test_group_sums_memory_and_labels_users(self):
        rows = [
            row(pid=2, name="chrome.exe", rss=50, username="bob", cpu_percent=1.0, private_working_set=40),
            row(pid=1, name="Chrome.exe", rss=100, username="alice", cpu_percent=2.5, private_working_set=80),
            row(pid=3, name="notepad.exe", rss=10, username="alice", private_working_set=10),
        ]
        grouped = group_rows(rows, total=1_000)
        by_name = {item.name: item for item in grouped}
        chrome = by_name["chrome.exe"]
        self.assertEqual(chrome.rss, 150)
        self.assertEqual(chrome.process_count, 2)
        self.assertEqual(chrome.pids, (1, 2))
        self.assertEqual(chrome.username, "2 users")
        self.assertEqual(chrome.private_working_set, 120)
        self.assertEqual(chrome.cpu_percent, 3.5)
        self.assertEqual(chrome.percent, 15.0)
        self.assertEqual(by_name["notepad.exe"].username, "alice")

    def test_equal_memory_sorts_by_name(self):
        rows = [
            row(name="zeta", rss=0, pid=2),
            row(name="alpha", rss=0, pid=1),
            row(name="mid", rss=50, pid=3),
        ]
        ordered = sort_rows(rows, "memory", descending=True, private_attr="private_bytes")
        self.assertEqual([item.name for item in ordered], ["mid", "alpha", "zeta"])

    def test_name_sort_is_alphabetical(self):
        rows = [row(name="b", pid=1), row(name="A", pid=2)]
        ordered = sort_rows(rows, "name", descending=False, private_attr="private_bytes")
        self.assertEqual([item.name for item in ordered], ["A", "b"])

    def test_min_memory_keeps_a_group_that_clears_the_bar(self):
        rows = [
            row(pid=1, name="helper.exe", rss=1024 * 1024),
            row(pid=2, name="helper.exe", rss=1024 * 1024),
        ]
        grouped = group_rows(rows, total=1024**3)
        kept = apply_min_memory(grouped, int(1.5 * 1024 * 1024))
        self.assertEqual(len(kept), 1)
        dropped = apply_min_memory(rows, int(1.5 * 1024 * 1024))
        self.assertEqual(dropped, [])

    def test_choose_private_attr_prefers_working_set(self):
        rows = [row(private_working_set=10, private_rss=5, private_bytes=7)]
        self.assertEqual(choose_private_attr(rows), "private_working_set")
        self.assertEqual(choose_private_attr([row(private_rss=5)]), "private_rss")

    def test_live_snapshot(self):
        snapshot = collect_snapshot(sample_cpu=False)
        self.assertGreater(snapshot.host.total, 0)
        self.assertGreaterEqual(snapshot.host.used, 0)
        self.assertGreaterEqual(len(snapshot.rows) + snapshot.skipped, 1)
        for item in snapshot.rows:
            self.assertGreaterEqual(item.rss, 0)
            self.assertGreaterEqual(item.percent, 0)
            if item.private_rss is not None:
                self.assertGreaterEqual(item.private_rss, 0)


if __name__ == "__main__":
    unittest.main()
