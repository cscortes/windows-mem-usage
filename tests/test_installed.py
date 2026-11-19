import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winmem.installed import (
    attach_process_memory,
    dedupe_programs,
    is_broad_location,
    is_under,
    parse_kb,
    parse_uninstall_entry,
    split_icon,
)
from winmem.models import InstalledProgram, ProcessRow
from pathlib import PureWindowsPath


def program(**kwargs) -> InstalledProgram:
    values = dict(
        name="App",
        version="1.0",
        publisher="Example",
        install_location=None,
        icon_name=None,
        estimated_size_kb=None,
    )
    values.update(kwargs)
    return InstalledProgram(**values)


def process(**kwargs) -> ProcessRow:
    values = dict(
        pid=1,
        name="app.exe",
        username="luis",
        status="running",
        rss=1_000,
        private_bytes=None,
        private_working_set=None,
        private_rss=None,
        percent=1.0,
        cpu_percent=None,
        exe=None,
    )
    values.update(kwargs)
    return ProcessRow(**values)


class ParseTests(unittest.TestCase):
    def test_reads_a_normal_program(self):
        parsed = parse_uninstall_entry(
            {
                "DisplayName": " Google Chrome ",
                "DisplayVersion": "131.0",
                "Publisher": "Google LLC",
                "InstallLocation": r"C:\Program Files\Google\Chrome\Application",
                "DisplayIcon": r"C:\Program Files\Google\Chrome\Application\chrome.exe,0",
                "EstimatedSize": "350,000",
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.name, "Google Chrome")
        self.assertEqual(parsed.version, "131.0")
        self.assertEqual(parsed.icon_name, "chrome.exe")
        self.assertEqual(parsed.estimated_size_kb, 350000)

    def test_skips_components_patches_and_blank_names(self):
        self.assertIsNone(parse_uninstall_entry({"DisplayName": "   "}))
        self.assertIsNone(parse_uninstall_entry({"DisplayName": "Hidden", "SystemComponent": 1}))
        self.assertIsNone(parse_uninstall_entry({"DisplayName": "Hidden", "SystemComponent": "1"}))
        self.assertIsNone(
            parse_uninstall_entry({"DisplayName": "Patch", "ReleaseType": "Security Update"})
        )
        self.assertIsNone(
            parse_uninstall_entry({"DisplayName": "Patch", "ParentDisplayName": "Windows"})
        )
        self.assertIsNone(parse_uninstall_entry({"DisplayName": "Security Update for Windows"}))
        self.assertIsNone(parse_uninstall_entry({"DisplayName": "Hotfix for Windows (KB1)"}))
        kept = parse_uninstall_entry({"DisplayName": "Update Manager", "Publisher": "Example"})
        self.assertIsNotNone(kept)

    def test_generic_icon_is_not_kept_as_an_identity(self):
        parsed = parse_uninstall_entry(
            {
                "DisplayName": "Old Tool",
                "DisplayIcon": r"C:\Windows\System32\msiexec.exe,0",
            }
        )
        assert parsed is not None
        self.assertIsNone(parsed.icon_name)

    def test_split_icon_and_size(self):
        path, name = split_icon(r'"C:\Program Files\App\app.exe",0')
        self.assertEqual(name, "app.exe")
        self.assertTrue(str(path).endswith("app.exe"))
        self.assertEqual(split_icon(None), (None, None))
        self.assertEqual(parse_kb(None), None)
        self.assertEqual(parse_kb(0), 0)
        self.assertEqual(parse_kb("1,024"), 1024)
        self.assertIsNone(parse_kb("nope"))

    def test_dedupe(self):
        first = program(name="Chrome", version="1", install_location=r"C:\Chrome")
        second = program(name="chrome", version="1", install_location=r"C:\Chrome")
        other = program(name="Chrome", version="2", install_location=r"C:\Chrome")
        self.assertEqual(len(dedupe_programs([first, second, other])), 2)


class MatchTests(unittest.TestCase):
    def test_longest_install_folder_wins(self):
        broad = program(name="Google", install_location=r"C:\Program Files\Google")
        specific = program(
            name="Chrome",
            install_location=r"C:\Program Files\Google\Chrome\Application",
            icon_name="chrome.exe",
        )
        proc = process(
            pid=10,
            name="chrome.exe",
            rss=5_000,
            private_working_set=4_000,
            exe=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        )
        helper = process(
            pid=11,
            name="chrome.exe",
            rss=500,
            private_working_set=100,
            exe=r"C:\Program Files\Google\Chrome\Application\Platform\helper.exe",
        )
        rows = attach_process_memory([broad, specific], [proc, helper], total=100_000)
        by_name = {row.name: row for row in rows}
        self.assertEqual(by_name["Chrome"].rss, 5_500)
        self.assertEqual(by_name["Chrome"].private_working_set, 4_100)
        self.assertEqual(by_name["Chrome"].pids, (10, 11))
        self.assertEqual(by_name["Google"].rss, 0)
        self.assertEqual(by_name["Google"].pids, ())
        self.assertEqual(sum(len(row.pids) for row in rows), 2)

    def test_path_match_beats_another_programs_icon(self):
        by_path = program(name="Chrome", install_location=r"C:\Apps\Chrome")
        by_icon = program(name="Other", icon_name="chrome.exe")
        proc = process(pid=4, name="chrome.exe", exe=r"C:\Apps\Chrome\chrome.exe", rss=80)
        rows = attach_process_memory([by_icon, by_path], [proc], total=1_000)
        by_name = {row.name: row for row in rows}
        self.assertEqual(by_name["Chrome"].pids, (4,))
        self.assertEqual(by_name["Other"].pids, ())

    def test_unique_icon_matches_when_there_is_no_folder(self):
        notepad = program(name="Notepad", icon_name="notepad.exe")
        proc = process(pid=8, name="notepad.exe", exe=r"C:\Windows\System32\notepad.exe", rss=30)
        rows = attach_process_memory([notepad], [proc], total=1_000)
        self.assertEqual(rows[0].pids, (8,))
        self.assertEqual(rows[0].rss, 30)

    def test_shared_or_generic_icons_do_not_match(self):
        first = program(name="One", icon_name="uninstall.exe")
        second = program(name="Two", icon_name="uninstall.exe")
        generic = program(name="Installer", icon_name="msiexec.exe")
        proc = process(pid=3, name="uninstall.exe", exe=r"C:\Apps\One\uninstall.exe")
        other = process(pid=4, name="msiexec.exe", exe=r"C:\Windows\System32\msiexec.exe")
        rows = attach_process_memory([first, second, generic], [proc, other], total=1_000)
        self.assertTrue(all(row.pids == () for row in rows))

    def test_broad_locations_are_not_used(self):
        folder = program(name="Everything", install_location=r"C:\Program Files")
        proc = process(pid=6, name="app.exe", exe=r"C:\Program Files\App\app.exe", rss=70)
        rows = attach_process_memory([folder], [proc], total=1_000)
        self.assertEqual(rows[0].rss, 0)
        self.assertTrue(is_broad_location(PureWindowsPath(r"C:\Program Files")))
        self.assertTrue(is_broad_location(PureWindowsPath(r"D:\Windows\System32")))
        self.assertFalse(is_broad_location(PureWindowsPath(r"C:\Program Files\App")))
        self.assertTrue(
            is_under(
                PureWindowsPath(r"C:\Program Files\App\app.exe"),
                PureWindowsPath(r"c:\program files\app"),
            )
        )
        self.assertFalse(
            is_under(
                PureWindowsPath(r"C:\Program Files\App\app.exe"),
                PureWindowsPath(r"C:\Program Files"),
            )
        )


if __name__ == "__main__":
    unittest.main()
