import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winmem.models import parse_build, windows_label
from winmem.render import format_bytes, memory_bar, truncate


class WindowsLabelTests(unittest.TestCase):
    def test_windows_11_build_replaces_legacy_product_name(self):
        label = windows_label("10", "10.0.22631", "Windows 10 Home", "23H2")
        self.assertEqual(label, "Windows 11 Home 23H2 (build 22631)")

    def test_windows_11_keeps_real_product_name(self):
        label = windows_label("10", "10.0.22631.2861", "Windows 11 Pro", "23H2")
        self.assertEqual(label, "Windows 11 Pro 23H2 (build 22631)")

    def test_windows_10_keeps_product_name(self):
        label = windows_label("10", "10.0.19045", "Windows 10 Home", "22H2")
        self.assertEqual(label, "Windows 10 Home 22H2 (build 19045)")

    def test_server_is_not_labeled_as_windows_11(self):
        label = windows_label("10", "10.0.26100", "Windows Server 2025 Standard", "24H2")
        self.assertEqual(label, "Windows Server 2025 Standard 24H2 (build 26100)")

    def test_missing_details(self):
        self.assertEqual(windows_label("", ""), "Windows")
        self.assertEqual(windows_label("11", ""), "Windows 11")

    def test_parse_build(self):
        self.assertEqual(parse_build("10.0.22631"), 22631)
        self.assertIsNone(parse_build("10.0"))
        self.assertIsNone(parse_build(""))


class FormatTests(unittest.TestCase):
    def test_binary_units(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1023), "1023 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(1024**2), "1.0 MB")
        self.assertEqual(format_bytes(1024**3), "1.0 GB")
        self.assertEqual(format_bytes(5 * 1024**4), "5.0 TB")
        self.assertEqual(format_bytes(None), "—")
        self.assertEqual(format_bytes(-20), "0 B")
        self.assertEqual(format_bytes(None, missing="-"), "-")

    def test_bar_and_truncate(self):
        self.assertEqual(memory_bar(50, plain=True, width=28), "[" + ("#" * 14) + ("-" * 14) + "]")
        self.assertEqual(memory_bar(0, plain=True, width=4), "[----]")
        self.assertEqual(memory_bar(100, plain=True, width=4), "[####]")
        self.assertEqual(truncate("chrome.exe", 20, plain=True), "chrome.exe")
        self.assertEqual(truncate("a" * 20, 10, plain=True), "aaaaaaa...")
        self.assertEqual(truncate("a" * 20, 10, plain=False), "aaaaaaaaa…")


if __name__ == "__main__":
    unittest.main()
