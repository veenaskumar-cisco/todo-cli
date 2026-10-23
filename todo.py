#!/usr/bin/env python3
"""A small command-line todo list stored in a readable text file."""

from __future__ import annotations

import argparse
import base64
import csv
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


TODO_FILE = Path(__file__).resolve().parent / "todo.txt"
FORMAT_LINE = "# todo-cli v2 next_id={next_id}"
COLUMN_LINE = "# record\tid\tstatus\tcreated\tcompleted\ttext"
LEGACY_COLUMN_LINE = "# id\tstatus\tcreated\tcompleted\tdescription"
FORMAT_PATTERN = re.compile(r"# todo-cli v([12]) next_id=([1-9][0-9]*)")


class TodoFileError(Exception):
    """Raised when todo.txt cannot be safely read."""


@dataclass
class TaskNote:
    created: datetime
    text: str


@dataclass
class PreviewLine:
    text: str
    style: str
    note_index: int | None = None


@dataclass
class Task:
    task_id: int
    status: str
    created: date
    completed: date | None
    description: str
    notes: list[TaskNote] = field(default_factory=list)


@dataclass
class TodoData:
    next_id: int
    tasks: list[Task]


def parse_date(value: str) -> date:
    """Parse an ISO date and raise a clear argparse error if it is invalid."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; use YYYY-MM-DD"
        ) from exc


def parse_task_id(value: str) -> int:
    """Parse a positive integer task ID."""
    try:
        task_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("task ID must be a positive integer") from exc
    if task_id < 1:
        raise argparse.ArgumentTypeError("task ID must be a positive integer")
    return task_id


def parse_task_row(row: list[str], line_number: int, version: int) -> Task:
    """Validate and convert one tab-separated task row."""
    expected_fields = 5 if version == 1 else 6
    if len(row) != expected_fields:
        raise TodoFileError(
            f"line {line_number}: expected {expected_fields} tab-separated "
            f"fields, found {len(row)}"
        )

    if version == 2:
        if row[0] != "task":
            raise TodoFileError(f"line {line_number}: unknown record type {row[0]!r}")
        row = row[1:]

    raw_id, status, raw_created, raw_completed, description = row
    try:
        task_id = parse_task_id(raw_id)
    except argparse.ArgumentTypeError as exc:
        raise TodoFileError(f"line {line_number}: {exc}") from exc

    if status not in {"pending", "in-progress", "done"}:
        raise TodoFileError(
            f"line {line_number}: status must be 'pending', 'in-progress', or 'done'"
        )

    try:
        created = date.fromisoformat(raw_created)
    except ValueError as exc:
        raise TodoFileError(
            f"line {line_number}: invalid creation date {raw_created!r}"
        ) from exc

    if status != "done":
        if raw_completed != "-":
            raise TodoFileError(
                f"line {line_number}: an incomplete task must use '-' "
                "as completion date"
            )
        completed = None
    else:
        try:
            completed = date.fromisoformat(raw_completed)
        except ValueError as exc:
            raise TodoFileError(
                f"line {line_number}: completed task has an invalid completion date"
            ) from exc

    if not description.strip():
        raise TodoFileError(f"line {line_number}: description cannot be empty")

    return Task(task_id, status, created, completed, description)


def parse_note_row(row: list[str], line_number: int) -> tuple[int, TaskNote]:
    """Validate one v2 note row."""
    if len(row) != 6:
        raise TodoFileError(
            f"line {line_number}: expected 6 tab-separated fields, found {len(row)}"
        )

    record_type, raw_id, status, raw_created, completed, text = row
    if record_type != "note":
        raise TodoFileError(
            f"line {line_number}: unknown record type {record_type!r}"
        )
    if status != "-" or completed != "-":
        raise TodoFileError(
            f"line {line_number}: note status and completion fields must be '-'"
        )
    try:
        task_id = parse_task_id(raw_id)
    except argparse.ArgumentTypeError as exc:
        raise TodoFileError(f"line {line_number}: {exc}") from exc
    try:
        created = datetime.fromisoformat(raw_created)
    except ValueError as exc:
        raise TodoFileError(
            f"line {line_number}: invalid note timestamp {raw_created!r}"
        ) from exc
    if not text.strip():
        raise TodoFileError(f"line {line_number}: note cannot be empty")
    return task_id, TaskNote(created, text)


def load_todos(path: Path = TODO_FILE) -> TodoData:
    """Read todo data, rejecting malformed content before any write occurs."""
    if not path.exists() or path.stat().st_size == 0:
        return TodoData(next_id=1, tasks=[])

    try:
        with path.open("r", encoding="utf-8", newline="") as todo_file:
            lines = todo_file.readlines()
    except OSError as exc:
        raise TodoFileError(f"could not read {path}: {exc}") from exc

    if not lines:
        return TodoData(next_id=1, tasks=[])

    format_match = FORMAT_PATTERN.fullmatch(lines[0].rstrip("\r\n"))
    if not format_match:
        raise TodoFileError("line 1: missing or invalid todo-cli format header")
    version = int(format_match.group(1))
    next_id = int(format_match.group(2))

    expected_header = LEGACY_COLUMN_LINE if version == 1 else COLUMN_LINE
    if len(lines) < 2 or lines[1].rstrip("\r\n") != expected_header:
        raise TodoFileError("line 2: missing or invalid column header")

    tasks: list[Task] = []
    seen_ids: set[int] = set()
    note_rows: list[tuple[int, TaskNote, int]] = []
    for line_number, line in enumerate(lines[2:], start=3):
        if not line.strip():
            continue
        try:
            row = next(csv.reader([line], delimiter="\t"))
        except csv.Error as exc:
            raise TodoFileError(f"line {line_number}: invalid tab-separated data") from exc
        if version == 2 and row and row[0] == "note":
            task_id, note = parse_note_row(row, line_number)
            note_rows.append((task_id, note, line_number))
            continue

        task = parse_task_row(row, line_number, version)
        if task.task_id in seen_ids:
            raise TodoFileError(
                f"line {line_number}: duplicate task ID {task.task_id}"
            )
        seen_ids.add(task.task_id)
        tasks.append(task)

    tasks_by_id = {task.task_id: task for task in tasks}
    for task_id, note, line_number in note_rows:
        task = tasks_by_id.get(task_id)
        if task is None:
            raise TodoFileError(
                f"line {line_number}: note refers to missing task ID {task_id}"
            )
        task.notes.append(note)

    if seen_ids and next_id <= max(seen_ids):
        raise TodoFileError(
            f"next_id must be greater than every task ID (use at least {max(seen_ids) + 1})"
        )

    return TodoData(next_id=next_id, tasks=tasks)


def save_todos(data: TodoData, path: Path = TODO_FILE) -> None:
    """Atomically replace todo.txt so an interrupted write cannot erase tasks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            temporary_file.write(FORMAT_LINE.format(next_id=data.next_id) + "\n")
            temporary_file.write(COLUMN_LINE + "\n")
            writer = csv.writer(
                temporary_file, delimiter="\t", lineterminator="\n"
            )
            for task in data.tasks:
                writer.writerow(
                    [
                        "task",
                        task.task_id,
                        task.status,
                        task.created.isoformat(),
                        task.completed.isoformat() if task.completed else "-",
                        task.description,
                    ]
                )
                for note in task.notes:
                    writer.writerow(
                        [
                            "note",
                            task.task_id,
                            "-",
                            note.created.isoformat(timespec="seconds"),
                            "-",
                            note.text,
                        ]
                    )
        os.replace(temporary_name, path)
    except OSError as exc:
        raise TodoFileError(f"could not write {path}: {exc}") from exc
    finally:
        if temporary_name and os.path.exists(temporary_name):
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def find_task(data: TodoData, task_id: int) -> Task | None:
    """Return the task with the requested ID, if it exists."""
    return next((task for task in data.tasks if task.task_id == task_id), None)


def ordinal(number: int) -> str:
    """Return a number with its English ordinal suffix."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def command_interactive() -> int:
    """Prompt for today's tasks until the user enters a blank line."""
    data = load_todos()
    today = date.today()
    new_tasks: list[Task] = []

    print(f"Today's todos ({today.isoformat()})")
    print("Enter one task at a time. Press Enter on an empty line to finish.")

    try:
        while True:
            description = input(f"{ordinal(len(new_tasks) + 1)} todo: ").strip()
            if not description:
                break
            if "\0" in description:
                print("Error: a task cannot contain a null character.", file=sys.stderr)
                continue

            task = Task(data.next_id, "pending", today, None, description)
            data.tasks.append(task)
            new_tasks.append(task)
            data.next_id += 1
    except EOFError:
        print()
    except KeyboardInterrupt:
        print("\nCancelled. No tasks were added.")
        return 130

    if not new_tasks:
        print("No tasks added.")
        return 0

    save_todos(data)
    task_ids = ", ".join(str(task.task_id) for task in new_tasks)
    noun = "task" if len(new_tasks) == 1 else "tasks"
    print(f"Added {noun} {task_ids} (created {today.isoformat()}).")
    return 0


def command_add(args: argparse.Namespace) -> int:
    if args.description is None:
        return command_interactive()

    description = args.description.strip()
    if not description:
        print("Error: task description cannot be empty.", file=sys.stderr)
        return 1
    if "\n" in description or "\r" in description or "\0" in description:
        print("Error: task description must fit on one text line.", file=sys.stderr)
        return 1

    data = load_todos()
    today = date.today()
    task = Task(data.next_id, "pending", today, None, description)
    data.tasks.append(task)
    data.next_id += 1
    save_todos(data)
    print(f"Added task {task.task_id} (created {task.created.isoformat()}).")
    return 0


def command_list(args: argparse.Namespace) -> int:
    data = load_todos()
    if args.all:
        tasks = data.tasks
        empty_message = "No tasks found."
    else:
        selected_date = args.date or date.today()
        tasks = [task for task in data.tasks if task.created == selected_date]
        if args.pending:
            tasks = [task for task in tasks if task.status == "pending"]
        qualifier = "pending tasks" if args.pending else "tasks"
        empty_message = f"No {qualifier} found for {selected_date.isoformat()}."

    if not tasks:
        print(empty_message)
        return 0

    print(f"{'ID':<6} {'STATUS':<12} {'CREATED':<12} {'COMPLETED':<12} DESCRIPTION")
    for task in tasks:
        completed = task.completed.isoformat() if task.completed else "-"
        print(
            f"{task.task_id:<6} {task.status:<12} "
            f"{task.created.isoformat():<12} {completed:<12} {task.description}"
        )
    return 0


def command_done(args: argparse.Namespace) -> int:
    data = load_todos()
    task = find_task(data, args.task_id)
    if task is None:
        print(f"Error: task ID {args.task_id} does not exist.", file=sys.stderr)
        return 1
    if task.status == "done":
        completed = task.completed.isoformat() if task.completed else "an unknown date"
        print(
            f"Task {task.task_id} is already completed ({completed}).",
            file=sys.stderr,
        )
        return 1

    task.status = "done"
    task.completed = date.today()
    save_todos(data)
    print(f"Completed task {task.task_id} on {task.completed.isoformat()}.")
    return 0


def command_start(args: argparse.Namespace) -> int:
    """Mark a task as in progress."""
    data = load_todos()
    task = find_task(data, args.task_id)
    if task is None:
        print(f"Error: task ID {args.task_id} does not exist.", file=sys.stderr)
        return 1
    if task.status == "in-progress":
        print(f"Task {task.task_id} is already in progress.", file=sys.stderr)
        return 1

    task.status = "in-progress"
    task.completed = None
    save_todos(data)
    print(f"Started task {task.task_id}.")
    return 0


def command_undo(args: argparse.Namespace) -> int:
    """Reopen a completed task."""
    data = load_todos()
    task = find_task(data, args.task_id)
    if task is None:
        print(f"Error: task ID {args.task_id} does not exist.", file=sys.stderr)
        return 1
    if task.status == "pending":
        print(f"Task {task.task_id} is already pending.", file=sys.stderr)
        return 1

    task.status = "pending"
    task.completed = None
    save_todos(data)
    print(f"Reopened task {task.task_id}.")
    return 0


def command_note(args: argparse.Namespace) -> int:
    """Append a timestamped note to a task."""
    data = load_todos()
    task = find_task(data, args.task_id)
    if task is None:
        print(f"Error: task ID {args.task_id} does not exist.", file=sys.stderr)
        return 1

    if args.text is None:
        try:
            text = input(f"Note for task {task.task_id}: ").strip()
        except EOFError:
            text = ""
    else:
        text = args.text.strip()

    if not text:
        print("Error: note cannot be empty.", file=sys.stderr)
        return 1
    if "\n" in text or "\r" in text or "\0" in text:
        print("Error: note must fit on one text line.", file=sys.stderr)
        return 1

    note = TaskNote(datetime.now(), text)
    task.notes.append(note)
    save_todos(data)
    print(
        f"Added note to task {task.task_id} at "
        f"{note.created.isoformat(timespec='seconds')}."
    )
    return 0


def command_delete(args: argparse.Namespace) -> int:
    data = load_todos()
    task_ids = list(dict.fromkeys(args.task_ids))
    tasks_by_id = {task.task_id: task for task in data.tasks}
    missing_ids = [task_id for task_id in task_ids if task_id not in tasks_by_id]
    if missing_ids:
        missing_text = ", ".join(str(task_id) for task_id in missing_ids)
        noun = "ID" if len(missing_ids) == 1 else "IDs"
        verb = "does" if len(missing_ids) == 1 else "do"
        print(
            f"Error: task {noun} {missing_text} {verb} not exist.",
            file=sys.stderr,
        )
        return 1
    tasks = [tasks_by_id[task_id] for task_id in task_ids]

    try:
        if len(tasks) == 1:
            task = tasks[0]
            prompt = f'Delete task {task.task_id} "{task.description}"? [y/N] '
        else:
            selected_ids = ", ".join(str(task.task_id) for task in tasks)
            prompt = f"Delete {len(tasks)} tasks ({selected_ids})? [y/N] "
        answer = input(prompt).strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes"}:
        print("Delete cancelled.")
        return 0

    selected_id_set = set(task_ids)
    data.tasks = [
        task for task in data.tasks if task.task_id not in selected_id_set
    ]
    save_todos(data)
    if len(tasks) == 1:
        print(f"Deleted task {tasks[0].task_id}.")
    else:
        deleted_ids = ", ".join(str(task.task_id) for task in tasks)
        print(f"Deleted tasks {deleted_ids}.")
    return 0


def command_edit(_args: argparse.Namespace) -> int:
    if not TODO_FILE.exists() or TODO_FILE.stat().st_size == 0:
        save_todos(TodoData(next_id=1, tasks=[]))

    editor = os.environ.get("EDITOR", "vim")
    try:
        editor_command = shlex.split(editor, posix=os.name != "nt")
    except ValueError as exc:
        print(f"Error: invalid EDITOR value: {exc}", file=sys.stderr)
        return 1
    if not editor_command:
        print("Error: EDITOR is empty.", file=sys.stderr)
        return 1

    try:
        result = subprocess.run([*editor_command, str(TODO_FILE)], check=False)
    except FileNotFoundError:
        print(
            f"Error: editor {editor_command[0]!r} was not found. "
            "Set the EDITOR environment variable to an installed editor.",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"Error: could not start editor: {exc}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(f"Editor exited with status {result.returncode}.", file=sys.stderr)
        return result.returncode

    try:
        load_todos()
    except TodoFileError as exc:
        print(
            f"Warning: the edited todo.txt is malformed: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


def fuzzy_match(query: str, text: str) -> bool:
    """Return True when query characters appear in text in the same order."""
    remaining = iter(text.casefold())
    return all(character in remaining for character in query.casefold())


def tui_visible_tasks(
    data: TodoData,
    view: str,
    query: str,
    selected_date: date | None = None,
) -> list[Task]:
    """Select tasks for a TUI view and optional fuzzy search."""
    if view == "all":
        tasks = list(data.tasks)
    else:
        view_date = selected_date if view == "date" else date.today()
        tasks = [task for task in data.tasks if task.created == view_date]
        if view == "pending":
            tasks = [task for task in tasks if task.status == "pending"]

    if query:
        tasks = [
            task
            for task in tasks
            if fuzzy_match(
                query,
                f"{task.task_id} {task.status} {task.created} {task.description}",
            )
        ]
    return tasks


def tui_write(screen, curses_module, row: int, column: int, text: str, width: int, attributes: int = 0) -> None:
    """Write clipped text without crashing at the terminal's bottom-right edge."""
    if row < 0 or column < 0 or width <= column:
        return
    try:
        screen.addnstr(row, column, text, max(0, width - column - 1), attributes)
    except curses_module.error:
        pass


def tui_prompt(screen, curses_module, label: str) -> str:
    """Read one line from the TUI's bottom row."""
    height, width = screen.getmaxyx()
    row = max(0, height - 1)
    screen.move(row, 0)
    screen.clrtoeol()
    tui_write(screen, curses_module, row, 0, label, width)
    screen.refresh()

    try:
        curses_module.echo()
        curses_module.curs_set(1)
    except curses_module.error:
        pass

    try:
        start = min(len(label), max(0, width - 2))
        raw_value = screen.getstr(row, start, max(1, width - start - 1))
        return raw_value.decode("utf-8", errors="replace").strip()
    finally:
        curses_module.noecho()
        try:
            curses_module.curs_set(0)
        except curses_module.error:
            pass


def tui_edit_text(
    screen, curses_module, label: str, initial_text: str
) -> str | None:
    """Edit prefilled text with cursor movement and session-local undo."""
    text = initial_text
    cursor = len(text)
    horizontal_offset = 0
    undo_stack: list[tuple[str, int]] = []

    curses_module.noecho()
    try:
        curses_module.curs_set(1)
    except curses_module.error:
        pass

    try:
        while True:
            height, width = screen.getmaxyx()
            row = max(0, height - 1)
            screen.move(row, 0)
            screen.clrtoeol()
            tui_write(screen, curses_module, row, 0, label, width)

            start = min(len(label), max(0, width - 2))
            available = max(1, width - start - 1)
            if cursor < horizontal_offset:
                horizontal_offset = cursor
            elif cursor > horizontal_offset + available:
                horizontal_offset = cursor - available

            visible_text = text[
                horizontal_offset : horizontal_offset + available
            ]
            tui_write(
                screen,
                curses_module,
                row,
                start,
                visible_text,
                width,
            )
            screen.move(row, min(width - 1, start + cursor - horizontal_offset))
            screen.refresh()

            try:
                key = screen.get_wch()
            except curses_module.error:
                continue

            if key in {"\n", "\r", getattr(curses_module, "KEY_ENTER", -1)}:
                return text.strip()
            if key == "\x1b":
                return None
            if key == "\x1a":
                if undo_stack:
                    text, cursor = undo_stack.pop()
                continue
            if key in {
                "\x08",
                "\x7f",
                getattr(curses_module, "KEY_BACKSPACE", -1),
            }:
                if cursor > 0:
                    undo_stack.append((text, cursor))
                    text = text[: cursor - 1] + text[cursor:]
                    cursor -= 1
                continue
            if key == getattr(curses_module, "KEY_DC", -1):
                if cursor < len(text):
                    undo_stack.append((text, cursor))
                    text = text[:cursor] + text[cursor + 1 :]
                continue
            if key == getattr(curses_module, "KEY_LEFT", -1):
                cursor = max(0, cursor - 1)
                continue
            if key == getattr(curses_module, "KEY_RIGHT", -1):
                cursor = min(len(text), cursor + 1)
                continue
            if key == getattr(curses_module, "KEY_HOME", -1) or key == "\x01":
                cursor = 0
                continue
            if key == getattr(curses_module, "KEY_END", -1) or key == "\x05":
                cursor = len(text)
                continue
            if isinstance(key, str) and key.isprintable():
                undo_stack.append((text, cursor))
                text = text[:cursor] + key + text[cursor:]
                cursor += len(key)
    finally:
        try:
            curses_module.curs_set(0)
        except curses_module.error:
            pass


def tui_theme(curses_module) -> dict[str, int]:
    """Create a Tokyo Night palette with a standard-color fallback."""
    theme = {
        "normal": curses_module.A_NORMAL,
        "background": curses_module.A_NORMAL,
        "title": curses_module.A_BOLD,
        "border": curses_module.A_NORMAL,
        "selected": curses_module.A_REVERSE,
        "done": curses_module.A_NORMAL,
        "progress": curses_module.A_BOLD,
        "muted": curses_module.A_NORMAL,
        "accent": curses_module.A_BOLD,
    }

    has_colors = getattr(curses_module, "has_colors", lambda: False)
    if not has_colors():
        return theme

    try:
        curses_module.start_color()
        curses_module.use_default_colors()
        if getattr(curses_module, "COLORS", 0) >= 256:
            background = 234
            foreground = 189
            cyan = 117
            purple = 141
            selection = 60
            green = 149
            blue = 111
            yellow = 179
        else:
            background = -1
            foreground = -1
            cyan = curses_module.COLOR_CYAN
            purple = curses_module.COLOR_MAGENTA
            selection = curses_module.COLOR_BLUE
            green = curses_module.COLOR_GREEN
            blue = curses_module.COLOR_BLUE
            yellow = curses_module.COLOR_YELLOW

        curses_module.init_pair(1, cyan, background)
        curses_module.init_pair(2, purple, background)
        curses_module.init_pair(3, foreground, selection)
        curses_module.init_pair(4, green, background)
        curses_module.init_pair(5, blue, background)
        curses_module.init_pair(6, yellow, background)
        curses_module.init_pair(7, foreground, background)
        theme.update(
            {
                "normal": curses_module.color_pair(7),
                "background": curses_module.color_pair(7),
                "title": curses_module.color_pair(2) | curses_module.A_BOLD,
                "border": curses_module.color_pair(1),
                "selected": curses_module.color_pair(3) | curses_module.A_BOLD,
                "done": curses_module.color_pair(4),
                "progress": curses_module.color_pair(6) | curses_module.A_BOLD,
                "muted": curses_module.color_pair(5),
                "accent": curses_module.color_pair(1) | curses_module.A_BOLD,
            }
        )
    except curses_module.error:
        pass
    return theme


def tui_box(
    screen,
    curses_module,
    top: int,
    left: int,
    height: int,
    width: int,
    title: str,
    attributes: int,
) -> None:
    """Draw a bordered pane with a title."""
    if height < 3 or width < 4:
        return
    horizontal = "─" * max(0, width - 2)
    tui_write(
        screen,
        curses_module,
        top,
        left,
        f"┌{horizontal}┐",
        left + width + 1,
        attributes,
    )
    for row in range(top + 1, top + height - 1):
        tui_write(screen, curses_module, row, left, "│", left + width, attributes)
        tui_write(
            screen,
            curses_module,
            row,
            left + width - 1,
            "│",
            left + width + 1,
            attributes,
        )
    tui_write(
        screen,
        curses_module,
        top + height - 1,
        left,
        f"└{horizontal}┘",
        left + width + 1,
        attributes,
    )
    if title:
        tui_write(
            screen,
            curses_module,
            top,
            left + 2,
            f" {title} ",
            left + width - 1,
            attributes | curses_module.A_BOLD,
        )


def tui_preview_lines(task: Task | None, width: int) -> list[PreviewLine]:
    """Build styled, selectable lines for the preview pane."""
    if task is None:
        return [PreviewLine("No task selected", "muted")]

    completed = task.completed.isoformat() if task.completed else "-"
    lines = [
        PreviewLine(f"{'ID':<10} {task.task_id}", "normal"),
        PreviewLine(f"{'Status':<10} {task.status}", "normal"),
        PreviewLine(f"{'Created':<10} {task.created.isoformat()}", "normal"),
        PreviewLine(f"{'Completed':<10} {completed}", "normal"),
        PreviewLine("", "normal"),
        PreviewLine("Description", "accent"),
        PreviewLine("", "normal"),
    ]
    lines.extend(
        PreviewLine(line, "normal")
        for line in textwrap.wrap(task.description, width=max(10, width))
    )

    if task.notes:
        lines.extend(
            [
                PreviewLine("", "normal"),
                PreviewLine(f"Notes / Log ({len(task.notes)})", "accent"),
                PreviewLine("", "normal"),
            ]
        )
    for note_index in range(len(task.notes) - 1, -1, -1):
        note = task.notes[note_index]
        timestamp = note.created.strftime("%Y-%m-%d %H:%M")
        lines.extend(
            PreviewLine(line, "normal", note_index)
            for line in textwrap.wrap(
                f"[{timestamp}] {note.text}", width=max(10, width)
            )
        )
        lines.append(PreviewLine("", "normal"))
    return lines


def tui_preview(
    screen,
    curses_module,
    lines: list[PreviewLine],
    top: int,
    left: int,
    height: int,
    width: int,
    theme: dict[str, int],
    focused: bool,
    cursor: int,
    visual_anchor: int | None,
    scroll_offset: int,
) -> int:
    """Render selectable preview lines and return the updated scroll offset."""
    if cursor < scroll_offset:
        scroll_offset = cursor
    elif cursor >= scroll_offset + height:
        scroll_offset = cursor - height + 1

    selection_start = cursor
    selection_end = cursor
    if visual_anchor is not None:
        selection_start = min(cursor, visual_anchor)
        selection_end = max(cursor, visual_anchor)

    for row_offset, preview_line in enumerate(
        lines[scroll_offset : scroll_offset + height]
    ):
        line_index = scroll_offset + row_offset
        attributes = theme.get(preview_line.style, theme["normal"])
        if focused and selection_start <= line_index <= selection_end:
            attributes = theme["selected"]
        tui_write(
            screen,
            curses_module,
            top + row_offset,
            left,
            preview_line.text,
            left + width,
            attributes,
        )
    return scroll_offset


def notes_for_copy(task: Task, latest_only: bool) -> str:
    """Format one or all task notes for the clipboard."""
    notes = task.notes[-1:] if latest_only else task.notes
    return "\n".join(
        f"[{note.created.strftime('%Y-%m-%d %H:%M:%S')}] {note.text}"
        for note in notes
    )


def copy_to_clipboard(text: str) -> str:
    """Copy text using a platform tool or an OSC 52 terminal sequence."""
    clipboard_commands = [
        (["wl-copy"], "wl-copy"),
        (["xclip", "-selection", "clipboard"], "xclip"),
        (["xsel", "--clipboard", "--input"], "xsel"),
        (["pbcopy"], "pbcopy"),
        (["clip.exe"], "clip.exe"),
    ]
    for command, name in clipboard_commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            result = subprocess.run(
                command,
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            continue
        if result.returncode == 0:
            return name

    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sequence = f"\033]52;c;{encoded}\a"
    if os.environ.get("TMUX"):
        escaped = sequence.replace(chr(27), chr(27) * 2)
        sequence = f"\033Ptmux;{escaped}\033\\"
    sys.stdout.write(sequence)
    sys.stdout.flush()
    return "terminal OSC 52"


def date_grouped_rows(
    data: TodoData, query: str
) -> list[tuple[str, Task | None]]:
    """Build date headers and task rows for the all-tasks dialog."""
    tasks = list(data.tasks)
    if query:
        tasks = [
            task
            for task in tasks
            if fuzzy_match(
                query,
                f"{task.task_id} {task.status} {task.created} {task.description}",
            )
        ]

    rows: list[tuple[str, Task | None]] = []
    dates = sorted({task.created for task in tasks}, reverse=True)
    for created_date in dates:
        rows.append((created_date.isoformat(), None))
        for task in tasks:
            if task.created == created_date:
                rows.append(("", task))
    return rows


def tui_space_action(screen, curses_module, theme: dict[str, int]) -> str:
    """Resolve Space, Space-Space, or Space-f-f without losing mark behavior."""
    set_timeout = getattr(screen, "timeout", None)
    if set_timeout is None:
        return "mark"

    height, width = screen.getmaxyx()
    tui_write(
        screen,
        curses_module,
        height - 1,
        0,
        "LEADER  f f: all tasks by date   Space: mark   Esc: cancel",
        width,
        theme["accent"],
    )
    screen.refresh()

    try:
        set_timeout(1000)
        try:
            second_key = screen.get_wch()
        except curses_module.error:
            return "mark"
        if second_key == " ":
            return "mark"
        if second_key == "\x1b":
            return "cancel"
        if second_key != "f":
            return "cancel"

        try:
            third_key = screen.get_wch()
        except curses_module.error:
            return "cancel"
        return "all-dialog" if third_key == "f" else "cancel"
    finally:
        set_timeout(-1)


def tui_all_tasks_dialog(
    screen, curses_module, data: TodoData, theme: dict[str, int]
) -> int | None:
    """Open a centered, date-grouped all-tasks picker."""
    query = ""
    search_focused = False
    selected_row = 0
    scroll_offset = 0

    while True:
        terminal_height, terminal_width = screen.getmaxyx()
        dialog_width = min(terminal_width - 4, max(60, int(terminal_width * 0.85)))
        dialog_height = min(
            terminal_height - 2, max(10, int(terminal_height * 0.85))
        )
        top = max(0, (terminal_height - dialog_height) // 2)
        left = max(0, (terminal_width - dialog_width) // 2)

        rows = date_grouped_rows(data, query)
        task_indices = [
            index for index, (_label, task) in enumerate(rows) if task is not None
        ]
        if task_indices:
            if selected_row not in task_indices:
                selected_row = task_indices[0]
        else:
            selected_row = 0

        for row in range(top, top + dialog_height):
            tui_write(
                screen,
                curses_module,
                row,
                left,
                " " * dialog_width,
                left + dialog_width + 1,
                theme["background"],
            )
        tui_box(
            screen,
            curses_module,
            top,
            left,
            dialog_height,
            dialog_width,
            "ALL TASKS BY DATE",
            theme["accent"],
        )

        list_top = top + 2
        list_height = max(1, dialog_height - 6)
        if selected_row < scroll_offset:
            scroll_offset = selected_row
        elif selected_row >= scroll_offset + list_height:
            scroll_offset = selected_row - list_height + 1

        if not rows:
            tui_write(
                screen,
                curses_module,
                list_top,
                left + 2,
                "No matching tasks",
                left + dialog_width - 1,
                theme["muted"],
            )
        for row_offset, (date_label, task) in enumerate(
            rows[scroll_offset : scroll_offset + list_height]
        ):
            row_index = scroll_offset + row_offset
            if task is None:
                text = f"── {date_label} ──"
                attributes = theme["accent"]
            else:
                marker = {
                    "pending": "[ ]",
                    "in-progress": "[~]",
                    "done": "[x]",
                }[task.status]
                text = f"{marker} {task.task_id:<5} {task.description}"
                attributes = (
                    theme["selected"]
                    if row_index == selected_row
                    else theme["done"]
                    if task.status == "done"
                    else theme["progress"]
                    if task.status == "in-progress"
                    else theme["normal"]
                )
            tui_write(
                screen,
                curses_module,
                list_top + row_offset,
                left + 2,
                text,
                left + dialog_width - 1,
                attributes,
            )

        search_text = f"> {query}_" if search_focused else f"> {query}"
        if not query and not search_focused:
            search_text = "> press / to fuzzy search"
        tui_write(
            screen,
            curses_module,
            top + dialog_height - 3,
            left + 2,
            search_text,
            left + dialog_width - 1,
            theme["accent"] if search_focused or query else theme["muted"],
        )
        help_text = (
            "type to filter  Enter accept  Esc clear"
            if search_focused
            else "j/k move  / search  Enter open  q/Esc close"
        )
        tui_write(
            screen,
            curses_module,
            top + dialog_height - 2,
            left + 2,
            help_text,
            left + dialog_width - 1,
            theme["muted"],
        )
        screen.refresh()

        try:
            key = screen.get_wch()
        except curses_module.error:
            continue

        if search_focused:
            if key in {"\n", "\r", getattr(curses_module, "KEY_ENTER", -1)}:
                search_focused = False
            elif key == "\x1b":
                query = ""
                search_focused = False
            elif key in {
                "\x08",
                "\x7f",
                getattr(curses_module, "KEY_BACKSPACE", -1),
            }:
                query = query[:-1]
                selected_row = 0
                scroll_offset = 0
            elif isinstance(key, str) and key.isprintable():
                query += key
                selected_row = 0
                scroll_offset = 0
            continue

        if key in {"q", "Q", "\x1b"}:
            return None
        if key == "/":
            search_focused = True
        elif key in {"\n", "\r", getattr(curses_module, "KEY_ENTER", -1)}:
            if rows and selected_row < len(rows):
                selected_task = rows[selected_row][1]
                if selected_task is not None:
                    return selected_task.task_id
        elif key in {curses_module.KEY_DOWN, "j"} and task_indices:
            position = task_indices.index(selected_row)
            selected_row = task_indices[min(position + 1, len(task_indices) - 1)]
        elif key in {curses_module.KEY_UP, "k"} and task_indices:
            position = task_indices.index(selected_row)
            selected_row = task_indices[max(position - 1, 0)]


def run_tui(screen, curses_module) -> int:
    """Run the full-screen keyboard interface."""
    data = load_todos()
    view = "today"
    selected_view_date = date.today()
    query = ""
    selected = 0
    scroll_offset = 0
    message = "Ready"
    focus = "tasks"
    preview_cursor = 0
    preview_scroll = 0
    preview_task_id: int | None = None
    visual_anchor: int | None = None
    marked_ids: set[int] = set()

    try:
        curses_module.curs_set(0)
    except curses_module.error:
        pass
    screen.keypad(True)
    theme = tui_theme(curses_module)
    set_background = getattr(screen, "bkgd", None)
    if set_background is not None:
        try:
            set_background(" ", theme["background"])
        except curses_module.error:
            pass

    while True:
        tasks = tui_visible_tasks(data, view, query, selected_view_date)
        if tasks:
            selected = min(selected, len(tasks) - 1)
        else:
            selected = 0
        selected_task = tasks[selected] if tasks else None
        current_task_id = selected_task.task_id if selected_task else None
        if current_task_id != preview_task_id:
            preview_task_id = current_task_id
            preview_cursor = 0
            preview_scroll = 0
            visual_anchor = None

        height, width = screen.getmaxyx()
        screen.erase()

        if height < 14 or width < 72:
            tui_write(
                screen,
                curses_module,
                0,
                0,
                "Terminal too small for themed view (minimum 72x14).",
                width,
                theme["title"],
            )
            tui_write(
                screen,
                curses_module,
                2,
                0,
                "Resize the terminal or press q to quit.",
                width,
                theme["muted"],
            )
            screen.refresh()
            key = screen.get_wch()
            if key in {"q", "Q"}:
                return 0
            continue

        view_name = {
            "today": "TODAY",
            "pending": "PENDING",
            "all": "ALL",
            "date": selected_view_date.isoformat(),
        }[view]

        pane_top = 0
        pane_height = height - pane_top - 2
        gap = 1
        left_width = max(32, int((width - gap) * 0.42))
        right_left = left_width + gap
        right_width = width - right_left
        search_height = 3
        results_height = pane_height - search_height
        search_top = pane_top + results_height

        tui_box(
            screen,
            curses_module,
            pane_top,
            0,
            results_height,
            left_width,
            f"{view_name} RESULTS {len(tasks)}"
            + (f" ({len(marked_ids)} MARKED)" if marked_ids else "")
            + (" [FOCUS]" if focus == "tasks" else ""),
            theme["accent"] if focus == "tasks" else theme["border"],
        )
        tui_box(
            screen,
            curses_module,
            search_top,
            0,
            search_height,
            left_width,
            "SEARCH" + (" [FOCUS]" if focus == "search" else ""),
            theme["accent"] if focus == "search" else theme["border"],
        )
        tui_box(
            screen,
            curses_module,
            pane_top,
            right_left,
            pane_height,
            right_width,
            "TASK PREVIEW" + (" [FOCUS]" if focus == "preview" else ""),
            theme["accent"] if focus == "preview" else theme["border"],
        )

        list_height = max(1, results_height - 2)
        if selected < scroll_offset:
            scroll_offset = selected
        elif selected >= scroll_offset + list_height:
            scroll_offset = selected - list_height + 1

        if not tasks:
            empty_text = "No matching tasks. Press a to add one."
            tui_write(
                screen,
                curses_module,
                pane_top + 1,
                2,
                empty_text,
                left_width - 1,
                theme["muted"],
            )
        else:
            for row_offset, task in enumerate(
                tasks[scroll_offset : scroll_offset + list_height]
            ):
                task_index = scroll_offset + row_offset
                marker = ">" if task_index == selected else " "
                marked = "*" if task.task_id in marked_ids else " "
                checkbox = {
                    "pending": "[ ]",
                    "in-progress": "[~]",
                    "done": "[x]",
                }[task.status]
                line = (
                    f"{marker}{marked} {task_index + 1:>2}. "
                    f"{checkbox} {task.description}"
                )
                attributes = (
                    theme["selected"]
                    if task_index == selected
                    else theme["done"]
                    if task.status == "done"
                    else theme["progress"]
                    if task.status == "in-progress"
                    else theme["normal"]
                )
                tui_write(
                    screen,
                    curses_module,
                    pane_top + 1 + row_offset,
                    1,
                    line,
                    left_width - 1,
                    attributes,
                )

        if focus == "search":
            search_text = f"> {query}_"
        else:
            search_text = f"> {query}" if query else "> press / to fuzzy search"
        tui_write(
            screen,
            curses_module,
            search_top + 1,
            2,
            search_text,
            left_width - 1,
            theme["accent"] if query or focus == "search" else theme["muted"],
        )

        preview_lines = tui_preview_lines(selected_task, right_width - 4)
        preview_cursor = min(preview_cursor, max(0, len(preview_lines) - 1))
        preview_scroll = tui_preview(
            screen,
            curses_module,
            preview_lines,
            pane_top + 2,
            right_left + 2,
            pane_height - 4,
            right_width - 4,
            theme,
            focus == "preview",
            preview_cursor,
            visual_anchor,
            preview_scroll,
        )

        footer_date = selected_view_date if view == "date" else date.today()
        date_text = footer_date.isoformat()
        status = message
        if marked_ids:
            status += f" | {len(marked_ids)} task(s) marked"
        if query:
            status += f" | search: {query}"
        tui_write(
            screen,
            curses_module,
            height - 2,
            0,
            status,
            width - len(date_text) - 2,
            theme["muted"],
        )
        tui_write(
            screen,
            curses_module,
            height - 2,
            width - len(date_text) - 1,
            date_text,
            width,
            theme["accent"],
        )
        if focus == "preview":
            help_text = (
                "PREVIEW  j/k move  e edit note  v visual  y yank  Esc cancel  "
                "Ctrl+H tasks  q quit"
            )
        elif focus == "search":
            help_text = (
                "SEARCH  type to filter  Backspace edit  Enter accept  "
                "A all  t today  p pending  Esc clear"
            )
        else:
            help_text = (
                "TASKS  j/k move  Space select / ff dates  x delete  Esc clear  "
                "Ctrl+L preview  a add  n note  s start  d done  u undo  "
                "/ search  q quit"
            )
        tui_write(
            screen,
            curses_module,
            height - 1,
            0,
            help_text,
            width,
            theme["accent"],
        )
        screen.refresh()

        try:
            key = screen.get_wch()
        except curses_module.error:
            continue

        if focus == "search":
            if key == "A":
                view = "all"
                query = ""
                focus = "tasks"
                selected = 0
                scroll_offset = 0
                message = "Showing all tasks"
            elif key == "t":
                view = "today"
                query = ""
                focus = "tasks"
                selected = 0
                scroll_offset = 0
                message = "Showing today's tasks"
            elif key == "p":
                view = "pending"
                query = ""
                focus = "tasks"
                selected = 0
                scroll_offset = 0
                message = "Showing today's pending tasks"
            elif key in {"\n", "\r", getattr(curses_module, "KEY_ENTER", -1)}:
                focus = "tasks"
                message = "Search accepted"
            elif key == "\x1b":
                query = ""
                focus = "tasks"
                selected = 0
                scroll_offset = 0
                message = "Search cleared"
            elif key in {
                "\x08",
                "\x7f",
                getattr(curses_module, "KEY_BACKSPACE", -1),
            }:
                query = query[:-1]
                selected = 0
                scroll_offset = 0
                message = "Search updated"
            elif key in {curses_module.KEY_DOWN}:
                if tasks:
                    selected = min(selected + 1, len(tasks) - 1)
            elif key in {curses_module.KEY_UP}:
                selected = max(selected - 1, 0)
            elif isinstance(key, str) and key.isprintable():
                query += key
                selected = 0
                scroll_offset = 0
                message = "Search updated"
            continue

        if key in {"q", "Q"}:
            return 0

        if key == "\x0c":
            if selected_task is None:
                message = "No task available for preview"
            else:
                focus = "preview"
                visual_anchor = None
                message = "Preview focused"
            continue

        if focus == "preview":
            if key in {"\x08", getattr(curses_module, "KEY_BACKSPACE", -1)}:
                focus = "tasks"
                visual_anchor = None
                message = "Task list focused"
            elif key in {curses_module.KEY_DOWN, "j"}:
                preview_cursor = min(
                    preview_cursor + 1, max(0, len(preview_lines) - 1)
                )
            elif key in {curses_module.KEY_UP, "k"}:
                preview_cursor = max(preview_cursor - 1, 0)
            elif key == "v":
                if visual_anchor is None:
                    visual_anchor = preview_cursor
                    message = "Visual selection started"
                else:
                    visual_anchor = None
                    message = "Visual selection cancelled"
            elif key == "\x1b":
                visual_anchor = None
                message = "Visual selection cancelled"
            elif key == "e":
                preview_line = preview_lines[preview_cursor]
                if preview_line.note_index is None or selected_task is None:
                    message = "Move onto a note line before pressing e"
                else:
                    note = selected_task.notes[preview_line.note_index]
                    replacement = tui_edit_text(
                        screen,
                        curses_module,
                        "Edit note [Ctrl+Z undo]: ",
                        note.text,
                    )
                    if replacement is None:
                        message = "Note edit cancelled"
                    elif not replacement:
                        message = "Note cannot be empty"
                    elif replacement != note.text:
                        note.text = replacement
                        save_todos(data)
                        message = "Note updated"
                    else:
                        message = "Note unchanged"
            elif key == "y":
                start = preview_cursor
                end = preview_cursor
                if visual_anchor is not None:
                    start = min(preview_cursor, visual_anchor)
                    end = max(preview_cursor, visual_anchor)
                copy_text = "\n".join(
                    line.text for line in preview_lines[start : end + 1]
                ).strip("\n")
                if copy_text:
                    method = copy_to_clipboard(copy_text)
                    line_count = end - start + 1
                    message = f"Yanked {line_count} line(s) using {method}"
                else:
                    message = "Selected line is empty"
                visual_anchor = None
            continue

        if key == " ":
            space_action = tui_space_action(screen, curses_module, theme)
            if space_action == "all-dialog":
                chosen_id = tui_all_tasks_dialog(
                    screen, curses_module, data, theme
                )
                if chosen_id is not None:
                    chosen_task = find_task(data, chosen_id)
                    if chosen_task is None:
                        message = f"Task {chosen_id} no longer exists"
                        continue
                    selected_view_date = chosen_task.created
                    view = "date"
                    query = ""
                    focus = "tasks"
                    day_tasks = tui_visible_tasks(
                        data, view, query, selected_view_date
                    )
                    selected = next(
                        (
                            index
                            for index, task in enumerate(day_tasks)
                            if task.task_id == chosen_id
                        ),
                        0,
                    )
                    scroll_offset = 0
                    message = (
                        f"Showing tasks from {selected_view_date.isoformat()}"
                    )
                else:
                    message = "All-tasks dialog closed"
            elif space_action == "mark" and tasks:
                task_id = tasks[selected].task_id
                if task_id in marked_ids:
                    marked_ids.remove(task_id)
                    message = f"Unmarked task {task_id}"
                else:
                    marked_ids.add(task_id)
                    message = f"Marked task {task_id}"
        elif key == "\x1b":
            if marked_ids:
                marked_ids.clear()
                message = "Cleared marked tasks"
        elif key in {curses_module.KEY_DOWN, "j"}:
            if tasks:
                selected = min(selected + 1, len(tasks) - 1)
        elif key in {curses_module.KEY_UP, "k"}:
            selected = max(selected - 1, 0)
        elif key == "a":
            description = tui_edit_text(
                screen, curses_module, "New todo: ", ""
            )
            if description:
                task = Task(data.next_id, "pending", date.today(), None, description)
                data.tasks.append(task)
                data.next_id += 1
                save_todos(data)
                message = f"Added task {task.task_id}"
            else:
                message = "Add cancelled"
        elif key == "s" and tasks:
            task = tasks[selected]
            if task.status == "in-progress":
                message = f"Task {task.task_id} is already in progress"
            else:
                task.status = "in-progress"
                task.completed = None
                save_todos(data)
                message = f"Started task {task.task_id}"
        elif key == "d" and tasks:
            task = tasks[selected]
            if task.status == "done":
                message = f"Task {task.task_id} is already done"
            else:
                task.status = "done"
                task.completed = date.today()
                save_todos(data)
                message = f"Completed task {task.task_id}"
        elif key == "u" and tasks:
            task = tasks[selected]
            if task.status == "pending":
                message = f"Task {task.task_id} is already pending"
            else:
                task.status = "pending"
                task.completed = None
                save_todos(data)
                message = f"Reopened task {task.task_id}"
        elif key == "n" and tasks:
            task = tasks[selected]
            text = tui_edit_text(
                screen,
                curses_module,
                f"Note for {task.task_id}: ",
                "",
            )
            if text:
                task.notes.append(TaskNote(datetime.now(), text))
                save_todos(data)
                message = f"Added note to task {task.task_id}"
            else:
                message = "Note cancelled"
        elif key in {"c", "y"} and tasks:
            task = tasks[selected]
            choice = tui_prompt(
                screen,
                curses_module,
                "Copy [d]escription, latest [n]ote, or [a]ll notes? ",
            ).casefold()
            copy_text = ""
            copied_item = ""
            if choice in {"d", "description"}:
                copy_text = task.description
                copied_item = "description"
            elif choice in {"n", "note"}:
                copy_text = notes_for_copy(task, latest_only=True)
                copied_item = "latest note"
            elif choice in {"a", "all"}:
                copy_text = notes_for_copy(task, latest_only=False)
                copied_item = "all notes"

            if copy_text:
                method = copy_to_clipboard(copy_text)
                message = f"Copied {copied_item} using {method}"
            elif choice in {"n", "note", "a", "all"}:
                message = "This task has no notes to copy"
            else:
                message = "Copy cancelled"
        elif key == "x" and (tasks or marked_ids):
            if marked_ids:
                delete_ids = sorted(marked_ids)
                answer = tui_prompt(
                    screen,
                    curses_module,
                    f"Delete {len(delete_ids)} marked tasks? [y/N] ",
                )
            else:
                task = tasks[selected]
                delete_ids = [task.task_id]
                answer = tui_prompt(
                    screen, curses_module, f"Delete task {task.task_id}? [y/N] "
                )
            if answer.casefold() in {"y", "yes"}:
                delete_id_set = set(delete_ids)
                data.tasks = [
                    task
                    for task in data.tasks
                    if task.task_id not in delete_id_set
                ]
                save_todos(data)
                marked_ids.difference_update(delete_id_set)
                if len(delete_ids) == 1:
                    message = f"Deleted task {delete_ids[0]}"
                else:
                    message = f"Deleted {len(delete_ids)} tasks"
            else:
                message = "Delete cancelled"
        elif key == "e" and tasks:
            task = tasks[selected]
            description = tui_edit_text(
                screen,
                curses_module,
                f"Edit task {task.task_id}: ",
                task.description,
            )
            if description is None:
                message = "Edit cancelled"
            elif not description:
                message = "Description cannot be empty"
            elif description != task.description:
                task.description = description
                save_todos(data)
                message = f"Edited task {task.task_id}"
            else:
                message = "Task unchanged"
        elif key == "/":
            focus = "search"
            message = "Search focused; type to filter"
        elif key == "t":
            view = "today"
            query = ""
            selected = 0
            scroll_offset = 0
            message = "Showing today's tasks"
        elif key == "p":
            view = "pending"
            query = ""
            selected = 0
            scroll_offset = 0
            message = "Showing today's pending tasks"
        elif key == "A":
            view = "all"
            query = ""
            selected = 0
            scroll_offset = 0
            message = "Showing all tasks"


def command_tui(_args: argparse.Namespace) -> int:
    """Open the Linux full-screen todo interface."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Error: todo ui requires an interactive terminal.", file=sys.stderr)
        return 1

    try:
        import curses
    except ImportError:
        print(
            "Error: this Python installation does not include curses. "
            "The full-screen interface is supported on Linux and macOS.",
            file=sys.stderr,
        )
        return 1

    try:
        return curses.wrapper(lambda screen: run_tui(screen, curses))
    except KeyboardInterrupt:
        return 130
    except curses.error as exc:
        print(f"Error: could not start the terminal interface: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo",
        description="Track daily tasks in a local, Vim-friendly todo.txt file.",
    )
    subparsers = parser.add_subparsers(dest="command")

    add_parser = subparsers.add_parser(
        "add", aliases=["a"], help="add a new task"
    )
    add_parser.add_argument("description", nargs="?", help="task description")
    add_parser.set_defaults(handler=command_add)

    list_parser = subparsers.add_parser(
        "list", aliases=["ls"], help="list tasks"
    )
    list_filter = list_parser.add_mutually_exclusive_group()
    list_filter.add_argument(
        "--pending", action="store_true", help="show today's pending tasks"
    )
    list_filter.add_argument(
        "--all", action="store_true", help="show tasks from every date"
    )
    list_filter.add_argument(
        "--date", type=parse_date, metavar="YYYY-MM-DD", help="show one date"
    )
    list_parser.set_defaults(handler=command_list)

    start_parser = subparsers.add_parser(
        "start", aliases=["s"], help="mark a task in progress"
    )
    start_parser.add_argument("task_id", type=parse_task_id, metavar="TASK-ID")
    start_parser.set_defaults(handler=command_start)

    done_parser = subparsers.add_parser("done", help="mark a task completed")
    done_parser.add_argument("task_id", type=parse_task_id, metavar="TASK-ID")
    done_parser.set_defaults(handler=command_done)

    undo_parser = subparsers.add_parser(
        "undo", aliases=["u"], help="reopen a completed task"
    )
    undo_parser.add_argument("task_id", type=parse_task_id, metavar="TASK-ID")
    undo_parser.set_defaults(handler=command_undo)

    note_parser = subparsers.add_parser(
        "note", aliases=["n", "log"], help="add a timestamped note to a task"
    )
    note_parser.add_argument("task_id", type=parse_task_id, metavar="TASK-ID")
    note_parser.add_argument("text", nargs="?", help="note text")
    note_parser.set_defaults(handler=command_note)

    delete_parser = subparsers.add_parser(
        "delete", aliases=["x"], help="delete one or more tasks"
    )
    delete_parser.add_argument(
        "task_ids", nargs="+", type=parse_task_id, metavar="TASK-ID"
    )
    delete_parser.set_defaults(handler=command_delete)

    edit_parser = subparsers.add_parser(
        "edit", aliases=["e"], help="open todo.txt in an editor"
    )
    edit_parser.set_defaults(handler=command_edit)

    tui_parser = subparsers.add_parser(
        "ui", aliases=["t"], help="open the full-screen terminal interface"
    )
    tui_parser.set_defaults(handler=command_tui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if not args.command:
            return command_interactive()
        return args.handler(args)
    except TodoFileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(
            "todo.txt was not changed. Fix the file in an editor and try again.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
