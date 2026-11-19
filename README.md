# Program memory

Shows why Windows 11 can report 13 GB in use while the process list adds up to less, and lists the missing pieces so the lines add back up to that 13 GB.

The Memory column in Task Manager is each process's private RAM. Three kinds of memory are inside the 13 GB and in no row of that column:

- Shared DLLs and memory-mapped files. They are in RAM once. The Memory column leaves them out. The full working set counts them again for every process, so that sum goes past 13 GB.
- The kernel: non-paged pool, paged pool that is in RAM, drivers, page tables, and kernel stacks. None of these is a program in the process list.
- Modified pages that have left a process and are waiting to be written out.

Compressed RAM is often a process named Memory Compression. Task Manager sometimes nests it under System, so it is easy to miss while still being a large part of the 13 GB.

The same command works on Windows 10, Linux, and macOS.

## Run it

Python 3.10 or newer. From this folder in PowerShell or Windows Terminal:

```powershell
py -3 -m pip install -r requirements.txt
py -3 list_programs.py
```

`python` and `python3` work in place of `py -3`. After `py -3 -m pip install -e .`, the `list-programs` command and `py -3 -m winmem` do the same thing. `py -3 list_programs.py --brief` prints only the breakdown.

The report is a point-in-time sample. Nothing is modified.

Format example, with sample numbers rather than a reading from a PC:

```
Where the RAM goes
desktop | Windows 11 Pro 23H2 (build 22631) | 8 CPUs
RAM in use  [#######################-----]  13.0 GB of 16.0 GB (81.2%) | 3.0 GB available

Windows reports 13.0 GB in use out of 16.0 GB.
The Memory column is each process's private RAM. Shared DLLs, the kernel, and pages
not assigned to one process are inside that in-use total and absent from every row,
so the column adds up short.

Where the 13.0 GB in use goes
  Process private RAM (142 processes)          8.0 GB
  Modified pages waiting to be written         1.0 GB
  Non-paged pool (kernel RAM kept in memory)   1.0 GB
  Paged pool in RAM                            1.0 GB
  Other memory in use                          2.0 GB
  ---------------------------------------------------
  In use                                      13.0 GB
These lines add up to RAM in use.

The other 3.0 GB is available
  Standby cache (ready for a program)  2.0 GB
  Free                                 1.0 GB
  -------------------------------------------
  Available                            3.0 GB
```

A live run fills those lines from the computer it is started on. The process list follows the breakdown. Private WS on Windows 11 is the Task Manager Memory column. It requires Windows 11 22H2 with the September 2023 update, or a later release.

## Columns

| Column | Meaning |
| --- | --- |
| Working set | Physical memory the process has mapped, including shared DLLs and files. On Linux and macOS this column is named RSS. |
| Private WS | Private working set, the Windows 11 Task Manager Memory column. Blank when Windows will not let this program query that process. |
| Running total | Private RAM added up through the current row, in the order the table is showing. The last row is the sum of that column. A blank private cell adds nothing. |
| Private RSS | On Linux and macOS, resident memory minus pages shared with other processes. |
| %RAM | Working set (or RSS) divided by installed RAM. |
| CPU% | Processor time over about a tenth of a second. A process can go past 100% when it uses more than one core. |
| RAM in use | Installed memory minus memory available for a new program. This is the same idea as Task Manager's in-use figure. |

Units are binary, matching Task Manager: 1 KB is 1024 bytes.

The breakdown above the process list is the part that adds up to RAM in use. The process list is only the first line of that breakdown. The Running total column is that first line being added, row by row. The line under the table states that total next to RAM in use. `--group` makes each row a program name before the running total continues.

`--verbose` adds status, the executable path, and private bytes. Private bytes are the commit charge, which can be larger than RAM because committed memory may sit in the page file. Commit charge is printed under the breakdown and is not part of the 13 GB.

## Other views

```powershell
py -3 list_programs.py --brief
py -3 list_programs.py --top 20
py -3 list_programs.py --group
py -3 list_programs.py --name chrome
py -3 list_programs.py --user luis
py -3 list_programs.py --min-mb 100
py -3 list_programs.py --watch 2
py -3 list_programs.py --json
py -3 list_programs.py --csv > memory.csv
py -3 list_programs.py --installed
py -3 list_programs.py --installed --running-only
py -3 list_programs.py --no-cpu
py -3 list_programs.py --plain
```

`--group` adds up every process with the same program name. Chrome's many `chrome.exe` processes become one row. `svchost.exe` also becomes one row, because those services share a name; leave `--group` off to see each process.

`--installed` reads the Windows uninstall registry (the 32-bit view, the 64-bit view, and the current user) and adds up processes running from each program's install folder. Programs that are not running are listed at 0. Updates, hotfixes, and system components are left out. Microsoft Store apps that never wrote an uninstall key still appear in the normal process list. This option is Windows-only.

`--json` prints the breakdown and the processes, in bytes. `--csv` prints the process rows only.

`--watch 2` pauses two seconds between reports. Stop it with Ctrl+C.

## If a number is missing

Some processes cannot be opened from a normal terminal. They are counted as inaccessible, and Private WS may be blank for them even when the working set is shown. Open Windows Terminal as Administrator and run the command again to fill in more of those values.

If the bars and dashes look wrong in an old console, run `py -3 list_programs.py --plain`. Set `NO_COLOR` or pass `--no-color` to turn colors off. Windows Terminal and PowerShell 7 show the normal layout.

## Tests

```powershell
py -3 -m pip install -r requirements.txt
py -3 -m unittest discover -s tests -v
```
