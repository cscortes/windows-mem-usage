import ctypes
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winmem.winapi import PROCESS_MEMORY_COUNTERS_EX2, read_private_working_sets


class WinApiTests(unittest.TestCase):
    def test_counter_layout_matches_the_64_bit_windows_struct(self):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("layout assertion is for 64-bit")
        self.assertEqual(ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX2), 96)
        self.assertEqual(PROCESS_MEMORY_COUNTERS_EX2.WorkingSetSize.offset, 16)
        self.assertEqual(PROCESS_MEMORY_COUNTERS_EX2.PrivateUsage.offset, 72)
        self.assertEqual(PROCESS_MEMORY_COUNTERS_EX2.PrivateWorkingSetSize.offset, 80)
        self.assertEqual(PROCESS_MEMORY_COUNTERS_EX2.SharedCommitUsage.offset, 88)

    def test_private_working_set_is_empty_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("Windows can return real values")
        self.assertEqual(read_private_working_sets([1, os.getpid()]), {})


if __name__ == "__main__":
    unittest.main()
