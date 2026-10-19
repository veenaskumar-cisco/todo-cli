# Daily Todo CLI

A small Python 3 command-line tool that stores daily tasks in a local,
Vim-friendly `todo.txt` file. It uses only the Python standard library.

## Project structure

```text
todo-cli/
├── todo.py       # The Python program
├── todo.bat      # Windows command launcher
├── todo          # macOS/Linux command launcher
├── test_todo.py  # Automated tests
└── todo.txt      # Created automatically when needed
```

## Text file format

`todo.txt` is tab-separated text:

```text
# todo-cli v1 next_id=3
# id	status	created	completed	description
1	pending	2026-10-19	-	Run BGP transit sanity on ifav210
2	done	2026-10-19	2026-10-19	Check remote-leaf SSH login issue
```

The first line stores the next unused ID. This makes IDs stable between runs
and prevents a deleted ID from being reused. The second line names the
columns. Each remaining line is one task. A pending task uses `-` for its
completion date.

You may edit this file in Vim. Keep the two header lines, use tabs between the
five task fields, and make `next_id` greater than every task ID. If a line is
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

### macOS or Linux

```sh
chmod +x todo
./todo add "Run BGP transit sanity on ifav210"
```

Optionally copy or link the `todo` launcher into a directory already on
`PATH`, such as `~/.local/bin`.

The data file is always `todo.txt` beside `todo.py`, so changing the terminal's
working directory does not create separate task lists.

## Commands

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
todo
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
