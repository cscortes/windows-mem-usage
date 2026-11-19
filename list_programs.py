#!/usr/bin/env python3
"""List running programs and their memory usage.

Works on Windows 11, and on any other operating system that psutil supports.
On Windows 11, the private working set matches Task Manager's Memory column.
"""

from winmem.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
