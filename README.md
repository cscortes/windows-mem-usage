# Program memory

Lists the programs running on a Windows 11 PC and how much memory each one is using. The same command works on Windows 10, Linux, and macOS.

On Windows 11, **Private WS** is the private working set. That is the Memory column in Task Manager. It is available on Windows 11 22H2 with the September 2023 update, and on later releases.

## Run it

Python 3.10 or newer. From this folder in PowerShell or Windows Terminal:

```powershell
py -3 -m pip install -r requirements.txt
py -3 list_programs.py
```

`python` and `python3` work in place of `py -3`. After `py -3 -m pip install -e .`, the `list-programs` command and `py -3 -m winmem` do the same thing.

The report is a point-in-time sample. Nothing is modified.

```
Running programs
desktop | Windows 11 Pro 23H2 (build 22631) | 8 CPUs
RAM in use  [##################----------]  10.0 GB of 16.0 GB (62.5%) | 6.0 GB available
Working set includes shared pages. Private WS is the Task Manager Memory column.

 PID  Program       User    Working set  Private WS   %RAM  CPU%
----  ------------  ------  -----------  ----------  -----  ----
4812  chrome.exe    luis         2.0 GB      1.4 GB  12.5%   3.2
 220  MsMpEng.exe   SYSTEM     700.0 MB    520.0 MB   4.3%   1.1
1104  explorer.exe  luis       280.0 MB    190.0 MB   1.7%   0.4

3 processes | 1 inaccessible | sorted by resident memory
Working sets sum to 3.0 GB. Shared pages count toward every process that maps them.
Process totals do not add up to RAM in use. That figure also includes the kernel and memory the system is holding.
Private working sets sum to 2.1 GB. Task Manager uses that private working set as its Memory column.
```

The rows above are a format example. A live run prints the processes on the computer where you start it.

## Columns

| Column | Meaning |
| --- | --- |
| Working set | Physical memory the process has mapped, including shared DLLs and files. On Linux and macOS this column is named RSS. |
| Private WS | Private working set, the Windows 11 Task Manager Memory column. Blank when Windows will not let this program query that process. |
| Private RSS | On Linux and macOS, resident memory minus pages shared with other processes. |
| %RAM | Working set (or RSS) divided by installed RAM. |
| CPU% | Processor time over about a tenth of a second. A process can go past 100% when it uses more than one core. |
| RAM in use | Installed memory minus memory available for a new program. This is the same idea as Task Manager's in-use figure. |

Units are binary, matching Task Manager: 1 KB is 1024 bytes.

Adding up every process does not equal RAM in use. Shared pages are counted in each process that maps them, and the system total also includes the kernel and memory Windows is holding. Private working set avoids the double count, and it is still smaller than RAM in use.

`--verbose` adds status, the executable path, and private bytes. Private bytes are the commit charge, which can be larger than RAM because committed memory may sit in the page file.

## Other views

```powershell
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

`--json` and `--csv` print bytes, not the rounded table text. CSV is the rows only. JSON also includes the computer's RAM totals.

`--watch 2` pauses two seconds between reports. Stop it with Ctrl+C.

## If a number is missing

Some processes cannot be opened from a normal terminal. They are counted as inaccessible, and Private WS may be blank for them even when the working set is shown. Open Windows Terminal as Administrator and run the command again to fill in more of those values.

If the bars and dashes look wrong in an old console, run `py -3 list_programs.py --plain`. Set `NO_COLOR` or pass `--no-color` to turn colors off. Windows Terminal and PowerShell 7 show the normal layout.

## Tests

```powershell
py -3 -m pip install -r requirements.txt
py -3 -m unittest discover -s tests -v
```
