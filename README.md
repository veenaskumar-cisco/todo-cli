# Daily Todo CLI

A small Python 3 command-line tool that stores daily tasks in a local,
Vim-friendly `todo.txt` file. It uses only the Python standard library.

## Project structure

```text
todo-cli/
├── todo.py       # The Python program
├── install.sh    # One-command Linux installer
├── todo.bat      # Windows command launcher
├── todo          # macOS/Linux command launcher
├── test_todo.py  # Automated tests
└── todo.txt      # Created automatically when needed
```

## Text file format

`todo.txt` is tab-separated text:

```text
# todo-cli v2 next_id=3
# record	id	status	created	completed	text
task	1	pending	2026-10-19	-	Run BGP transit sanity on ifav210
note	1	-	2026-10-19T12:15:00	-	Checked ifav210 logs
task	2	done	2026-10-19	2026-10-19	Check remote-leaf SSH login issue
```

The first line stores the next unused ID. This makes IDs stable between runs
and prevents a deleted ID from being reused. The second line names the
columns. A `task` line stores a task, while a `note` line stores a timestamped
note for the task ID in its second field. A pending task uses `-` for its
completion date.

You may edit this file in Vim. Keep the two header lines, use tabs between the
six fields, and make `next_id` greater than every task ID. If a line is
invalid, the program reports it and refuses to rewrite the file.

## Setup

Python 3 must be installed.

### Windows PowerShell

Run directly from this directory:

```powershell
.\todo.bat
.\todo.bat add "Run BGP transit sanity on ifav210"
```

To use `todo` from any directory for the current PowerShell session:

```powershell
$env:Path += ";C:\Users\veesenth\todo-cli"
todo add "Run BGP transit sanity on ifav210"
```

Add `C:\Users\veesenth\todo-cli` to the Windows user `Path` environment
variable to make that command permanent.

### Linux: install the `todo` command

```sh
curl -fsSL https://raw.githubusercontent.com/veenaskumar-cisco/todo-cli/v1.6.0/install.sh | sh
```

The installer uses no `sudo`. It installs the command at
`~/.local/bin/todo` and the program at
`~/.local/share/todo-cli/todo.py`. If `~/.local/bin` is not already on
`PATH`, the installer prints the one line to add to your shell configuration.

Then use it like a normal command:

```sh
todo
todo list
```

The installed data file is
`~/.local/share/todo-cli/todo.txt`. Changing the terminal's working directory
does not create separate task lists.

## Commands

Run `todo` by itself for a quick interactive prompt. Enter one task at a time;
press Enter on an empty prompt to save and finish. Every entered task
automatically receives today's local date:

```console
$ todo
Today's todos (2026-10-19)
Enter one task at a time. Press Enter on an empty line to finish.
1st todo: Run BGP transit sanity on ifav210
2nd todo: Check remote-leaf SSH login issue
3rd todo:
Added tasks 1, 2 (created 2026-10-19).
```

Short commands are available:

```console
todo a "Run BGP sanity test"  # add
todo a                        # interactive add
todo ls                       # list
todo n 1 "Checked logs"       # add a note to task 1
todo u 1                      # reopen completed task 1
todo x 1                      # delete task 1
todo e                        # edit todo.txt
todo t                        # full-screen terminal UI
```

### Full-screen terminal UI

On Linux or macOS, run:

```console
todo t
```

The Telescope-inspired dark interface has bordered results and preview panes,
a persistent fuzzy-search box, selected-task details, and timestamped notes.
It starts with today's tasks and supports:

- `j` / `k` or arrow keys: move between tasks
- `a`: add a task
- `n`: add a timestamped note/log to the selected task
- `d`: mark the selected task complete
- `u`: reopen the selected completed task
- `x`: delete the selected task after confirmation
- `e`: edit the selected task's description
- `/`: fuzzy-search the current view; submit a blank search to clear it
- `t`: show today's tasks
- `p`: show today's pending tasks
- `A`: show all tasks
- `q`: quit

`todo ui` is the long form of `todo t`. The interface uses Python's standard
`curses` module and reads and writes the same `todo.txt` file as every other
command.

Add tasks. The program supplies today's local date:

```console
todo add "Run BGP transit sanity on ifav210"
todo add "Check remote-leaf SSH login issue"
todo add "Send a test status update to my team"
todo add "Study Linux command-line basics"
```

Show all tasks created today, including completed tasks:

```console
todo list
```

Show only today's pending tasks:

```console
todo list --pending
```

Show pending and completed tasks from every date:

```console
todo list --all
```

Show tasks created on one date:

```console
todo list --date 2026-10-19
```

Complete a task using its ID:

```console
todo done 1
```

Reopen a completed task and clear its completion date:

```console
todo undo 1
todo u 1
```

Add a timestamped note or log entry to a task:

```console
todo note 1 "Checked interface counters"
todo n 1 "BGP session recovered"
todo log 1
```

When note text is omitted, the command prompts for it.

Delete a task after an interactive confirmation:

```console
todo delete 1
```

Open the text file with `$EDITOR`, or Vim when `$EDITOR` is unset:

```console
todo edit
```

On PowerShell, an editor can be selected for the current session with:

```powershell
$env:EDITOR = "vim"
```

Show help:

```console
todo --help
```

## How the program works

1. `argparse` validates commands, positive integer IDs, and ISO dates.
2. `load_todos()` validates the complete file before a command changes data.
3. `date.today()` supplies the computer's local date for add and done.
4. `save_todos()` writes a temporary file and atomically replaces `todo.txt`,
   avoiding a partly written file if saving is interrupted.
5. `next_id` is incremented after every add and is not reduced by deletion.

## Run the tests

From the project directory:

```console
python -m unittest -v
```
