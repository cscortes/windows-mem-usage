import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winmem.collect import collect_snapshot
from winmem.counters import balloon_bytes, file_cache_in_use, parse_meminfo
from winmem.ledger import MemoryFacts, build_ledger, facts_from_snapshot
from winmem.models import HostInfo, ProcessRow, Snapshot
from winmem.render import Style, render_process_report

GB = 1024**3


def facts(**kwargs) -> MemoryFacts:
    values = dict(
        total=16 * GB,
        available=3 * GB,
        system="Windows",
        process_private=8 * GB,
        process_count=142,
        working_set=12 * GB,
        skipped_processes=0,
    )
    values.update(kwargs)
    return MemoryFacts(**values)


class LedgerTests(unittest.TestCase):
    def test_windows_breakdown_adds_up_to_ram_in_use(self):
        ledger = build_ledger(
            facts(
                nonpaged_pool=1 * GB,
                paged_pool_resident=1 * GB,
                system_code_resident=GB // 2,
                system_driver_resident=GB // 2,
                system_cache_resident=GB // 2,
                modified=GB // 2,
                standby=2 * GB,
                free_bytes=1 * GB,
                commit=20 * GB,
                commit_limit=32 * GB,
            )
        )
        self.assertTrue(ledger.closes())
        self.assertEqual(ledger.in_use, 13 * GB)
        by_key = {line.key: line.amount for line in ledger.in_use_lines}
        self.assertEqual(by_key["process_private"], 8 * GB)
        self.assertEqual(by_key["nonpaged_pool"], 1 * GB)
        self.assertEqual(by_key["other"], 1 * GB)
        self.assertEqual(sum(line.amount for line in ledger.available_lines), 3 * GB)
        self.assertFalse(ledger.available_was_scaled)

    def test_unknown_kernel_memory_is_the_closing_line(self):
        ledger = build_ledger(facts(process_private=8 * GB, process_count=10))
        self.assertTrue(ledger.closes())
        self.assertEqual(ledger.in_use_lines[0].key, "process_private")
        self.assertEqual(ledger.in_use_lines[-1].key, "other")
        self.assertEqual(ledger.in_use_lines[-1].amount, 5 * GB)

    def test_overlapping_counter_is_not_given_its_own_line(self):
        ledger = build_ledger(facts(process_private=8 * GB, nonpaged_pool=6 * GB))
        self.assertTrue(ledger.closes())
        self.assertNotIn("nonpaged_pool", {line.key for line in ledger.in_use_lines})
        self.assertTrue(ledger.omitted)
        self.assertEqual(sum(line.amount for line in ledger.in_use_lines), 13 * GB)

    def test_private_ram_above_in_use_is_capped_so_the_sum_still_closes(self):
        ledger = build_ledger(facts(process_private=14 * GB, nonpaged_pool=GB))
        self.assertTrue(ledger.closes())
        private = next(line for line in ledger.in_use_lines if line.key == "process_private")
        self.assertEqual(private.amount, 13 * GB)
        self.assertEqual(ledger.process_private_measured, 14 * GB)

    def test_available_shortfall_and_overshoot(self):
        short = build_ledger(facts(standby=1 * GB, free_bytes=1 * GB))
        self.assertTrue(short.closes())
        self.assertEqual(sum(line.amount for line in short.available_lines), 3 * GB)
        self.assertIn("available_other", {line.key for line in short.available_lines})

        over = build_ledger(facts(standby=5 * GB, free_bytes=5 * GB))
        self.assertTrue(over.closes())
        self.assertTrue(over.available_was_scaled)
        self.assertEqual(sum(line.amount for line in over.available_lines), 3 * GB)

    def test_hardware_reserved_sits_outside_the_managed_total(self):
        ledger = build_ledger(facts(installed=17 * GB))
        self.assertEqual(ledger.hardware_reserved, 1 * GB)
        self.assertTrue(ledger.closes())

    def test_render_tells_why_the_column_is_short(self):
        ledger = build_ledger(
            facts(
                nonpaged_pool=1 * GB,
                modified=1 * GB,
                standby=2 * GB,
                free_bytes=1 * GB,
                commit=18 * GB,
                commit_limit=24 * GB,
                skipped_processes=2,
            )
        )
        report = render_process_report(
            HostInfo("desktop", "Windows 11 Pro 23H2 (build 22631)", "Windows", 16 * GB, 3 * GB, 13 * GB, 81.25, 8),
            [],
            grouped=False,
            matched=0,
            matched_processes=0,
            matched_rss=0,
            matched_private_ws=None,
            matched_private_rss=None,
            skipped=2,
            sort_field="private",
            truncated=False,
            verbose=False,
            style=Style(plain=True),
            ledger=ledger,
            brief=True,
        )
        self.assertIn("adds up short", report)
        self.assertIn("Memory Compression", report)
        self.assertIn("Process private RAM (142 processes)", report)
        self.assertIn("Other memory in use", report)
        self.assertIn("These lines add up to RAM in use.", report)
        self.assertIn("Commit charge is", report)
        self.assertIn("2 processes could not be queried", report)
        self.assertNotIn("PID", report)
        self.assertTrue(ledger.closes())

    def test_balloon_is_its_own_line_and_not_the_unnamed_remainder(self):
        ledger = build_ledger(facts(system="Linux", balloon=10 * GB, process_private=GB // 2, process_count=38))
        self.assertTrue(ledger.closes())
        by_key = {line.key: line.amount for line in ledger.in_use_lines}
        self.assertEqual(by_key["balloon"], 10 * GB)
        self.assertEqual(by_key["other"], 13 * GB - 10 * GB - GB // 2)

    def test_balloon_that_overshoots_the_room_left_is_still_named(self):
        ledger = build_ledger(facts(system="Linux", process_private=4 * GB, balloon=10 * GB))
        self.assertTrue(ledger.closes())
        balloon = next(line for line in ledger.in_use_lines if line.key == "balloon")
        self.assertEqual(balloon.amount, 9 * GB)
        self.assertEqual(ledger.balloon, 10 * GB)

    def test_balloon_pages_and_file_cache_math(self):
        self.assertEqual(balloon_bytes(2621440, 0, 4096), 10 * GB)
        self.assertEqual(balloon_bytes(5, 2, 4096), 3 * 4096)
        self.assertEqual(file_cache_in_use(cached=500, buffers=100, available=800, free=400), 200)
        self.assertEqual(file_cache_in_use(cached=100, buffers=0, available=800, free=400), 0)

    def test_meminfo_parser_uses_binary_kilobytes(self):
        info = parse_meminfo(
            "\n".join(
                [
                    "MemTotal:       16384 kB",
                    "KernelStack:     2048 kB",
                    "PageTables:      1024 kB",
                    "SUnreclaim:      4096 kB",
                    "NotANumber:      n/a",
                ]
            )
        )
        self.assertEqual(info["MemTotal"], 16384 * 1024)
        self.assertEqual(info["KernelStack"], 2048 * 1024)
        self.assertNotIn("NotANumber", info)

    def test_live_ledger_closes(self):
        snapshot = collect_snapshot(sample_cpu=False)
        ledger = build_ledger(facts_from_snapshot(snapshot))
        self.assertGreater(ledger.total, 0)
        self.assertTrue(ledger.closes())
        self.assertEqual(ledger.in_use + ledger.available, snapshot.host.total)
        if snapshot.host.system == "Linux" and ledger.balloon:
            named = {line.key for line in ledger.in_use_lines}
            self.assertIn("balloon", named)
            other = next((line.amount for line in ledger.in_use_lines if line.key == "other"), 0)
            self.assertLess(other, ledger.balloon)


class SnapshotLedgerTests(unittest.TestCase):
    def test_private_resident_pages_are_what_the_process_line_uses(self):
        host = HostInfo("build", "Linux 6.8.0", "Linux", 1000, 400, 600, 60.0, 2)
        rows = (
            ProcessRow(1, "a", "u", "running", 300, None, None, 200, 30.0, None, None),
            ProcessRow(2, "b", "u", "running", 100, None, None, None, 10.0, None, None),
        )
        ledger = build_ledger(facts_from_snapshot(Snapshot(host, rows, skipped=1)))
        private = next(line for line in ledger.in_use_lines if line.key == "process_private")
        self.assertEqual(private.amount, 200)
        self.assertIn("1 process", private.label)
        self.assertTrue(ledger.closes())


if __name__ == "__main__":
    unittest.main()
