#!/usr/bin/env python3
"""A small command-line todo list stored in a readable text file."""

from __future__ import annotations

import argparse
import csv
import os
import re
import shlex
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

    if status not in {"pending", "done"}:
        raise TodoFileError(
            f"line {line_number}: status must be 'pending' or 'done'"
        )

    try:
        created = date.fromisoformat(raw_created)
    except ValueError as exc:
        raise TodoFileError(
            f"line {line_number}: invalid creation date {raw_created!r}"
        ) from exc

    if status == "pending":
        if raw_completed != "-":
            raise TodoFileError(
                f"line {line_number}: a pending task must use '-' as completion date"
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

    print(f"{'ID':<6} {'STATUS':<9} {'CREATED':<12} {'COMPLETED':<12} DESCRIPTION")
    for task in tasks:
        completed = task.completed.isoformat() if task.completed else "-"
        print(
            f"{task.task_id:<6} {task.status:<9} "
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
    task = find_task(data, args.task_id)
    if task is None:
        print(f"Error: task ID {args.task_id} does not exist.", file=sys.stderr)
        return 1

    try:
        answer = input(
            f'Delete task {task.task_id} "{task.description}"? [y/N] '
        ).strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes"}:
        print("Delete cancelled.")
        return 0

    data.tasks.remove(task)
    save_todos(data)
    print(f"Deleted task {task.task_id}.")
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


def tui_visible_tasks(data: TodoData, view: str, query: str) -> list[Task]:
    """Select tasks for a TUI view and optional fuzzy search."""
    if view == "all":
        tasks = list(data.tasks)
    else:
        tasks = [task for task in data.tasks if task.created == date.today()]
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


def tui_theme(curses_module) -> dict[str, int]:
    """Create a dark Telescope-inspired color palette when colors are available."""
    theme = {
        "normal": curses_module.A_NORMAL,
        "title": curses_module.A_BOLD,
        "border": curses_module.A_NORMAL,
        "selected": curses_module.A_REVERSE,
        "done": curses_module.A_NORMAL,
        "muted": curses_module.A_NORMAL,
        "accent": curses_module.A_BOLD,
    }

    has_colors = getattr(curses_module, "has_colors", lambda: False)
    if not has_colors():
        return theme

    try:
        curses_module.start_color()
        curses_module.use_default_colors()
        curses_module.init_pair(1, curses_module.COLOR_CYAN, -1)
        curses_module.init_pair(2, curses_module.COLOR_MAGENTA, -1)
        curses_module.init_pair(3, curses_module.COLOR_BLACK, curses_module.COLOR_CYAN)
        curses_module.init_pair(4, curses_module.COLOR_GREEN, -1)
        curses_module.init_pair(5, curses_module.COLOR_BLUE, -1)
        theme.update(
            {
                "title": curses_module.color_pair(2) | curses_module.A_BOLD,
                "border": curses_module.color_pair(1),
                "selected": curses_module.color_pair(3) | curses_module.A_BOLD,
                "done": curses_module.color_pair(4),
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


def tui_preview(
    screen,
    curses_module,
    task: Task | None,
    top: int,
    left: int,
    height: int,
    width: int,
    theme: dict[str, int],
) -> None:
    """Render details for the selected task."""
    if task is None:
        tui_write(
            screen,
            curses_module,
            top,
            left,
            "No task selected",
            left + width,
            theme["muted"],
        )
        return

    completed = task.completed.isoformat() if task.completed else "-"
    details = [
        ("ID", str(task.task_id)),
        ("Status", task.status),
        ("Created", task.created.isoformat()),
        ("Completed", completed),
    ]
    row = top
    for label, value in details:
        tui_write(
            screen,
            curses_module,
            row,
            left,
            f"{label:<10}",
            left + width,
            theme["accent"],
        )
        tui_write(
            screen,
            curses_module,
            row,
            left + 11,
            value,
            left + width,
            theme["normal"],
        )
        row += 1

    row += 1
    tui_write(
        screen,
        curses_module,
        row,
        left,
        "Description",
        left + width,
        theme["accent"],
    )
    row += 2
    for line in textwrap.wrap(task.description, width=max(10, width - 1)):
        if row >= top + height:
            break
        tui_write(
            screen,
            curses_module,
            row,
            left,
            line,
            left + width,
            theme["normal"],
        )
        row += 1

    if task.notes and row < top + height - 1:
        row += 1
        tui_write(
            screen,
            curses_module,
            row,
            left,
            f"Notes / Log ({len(task.notes)})",
            left + width,
            theme["accent"],
        )
        row += 1

    for note in reversed(task.notes):
        if row >= top + height:
            break
        timestamp = note.created.strftime("%Y-%m-%d %H:%M")
        note_lines = textwrap.wrap(
            f"[{timestamp}] {note.text}", width=max(10, width - 1)
        )
        for line in note_lines:
            if row >= top + height:
                break
            tui_write(
                screen,
                curses_module,
                row,
                left,
                line,
                left + width,
                theme["normal"],
            )
            row += 1
        row += 1


def run_tui(screen, curses_module) -> int:
    """Run the full-screen keyboard interface."""
    data = load_todos()
    view = "today"
    query = ""
    selected = 0
    scroll_offset = 0
    message = "Ready"

    try:
        curses_module.curs_set(0)
    except curses_module.error:
        pass
    screen.keypad(True)
    theme = tui_theme(curses_module)

    while True:
        tasks = tui_visible_tasks(data, view, query)
        if tasks:
            selected = min(selected, len(tasks) - 1)
        else:
            selected = 0

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
            f"{view_name} RESULTS {len(tasks)}",
            theme["border"],
        )
        tui_box(
            screen,
            curses_module,
            search_top,
            0,
            search_height,
            left_width,
            "SEARCH",
            theme["border"],
        )
        tui_box(
            screen,
            curses_module,
            pane_top,
            right_left,
            pane_height,
            right_width,
            "TASK PREVIEW",
            theme["border"],
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
                checkbox = "[x]" if task.status == "done" else "[ ]"
                line = f"{marker} {checkbox} {task.task_id:<4} {task.description}"
                attributes = (
                    theme["selected"]
                    if task_index == selected
                    else theme["done"]
                    if task.status == "done"
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

        search_text = f"> {query}" if query else "> press / to fuzzy search"
        tui_write(
            screen,
            curses_module,
            search_top + 1,
            2,
            search_text,
            left_width - 1,
            theme["accent"] if query else theme["muted"],
        )

        selected_task = tasks[selected] if tasks else None
        tui_preview(
            screen,
            curses_module,
            selected_task,
            pane_top + 2,
            right_left + 2,
            pane_height - 4,
            right_width - 4,
            theme,
        )

        date_text = date.today().isoformat()
        status = message
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
        help_text = (
            "j/k move  a add  n note  d done  u undo  x delete  e edit  / search  "
            "t today  p pending  A all  q quit"
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

        if key in {"q", "Q"}:
            return 0
        if key in {curses_module.KEY_DOWN, "j"}:
            if tasks:
                selected = min(selected + 1, len(tasks) - 1)
        elif key in {curses_module.KEY_UP, "k"}:
            selected = max(selected - 1, 0)
        elif key == "a":
            description = tui_prompt(screen, curses_module, "New todo: ")
            if description:
                task = Task(data.next_id, "pending", date.today(), None, description)
                data.tasks.append(task)
                data.next_id += 1
                save_todos(data)
                message = f"Added task {task.task_id}"
            else:
                message = "Add cancelled"
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
            text = tui_prompt(screen, curses_module, f"Note for {task.task_id}: ")
            if text:
                task.notes.append(TaskNote(datetime.now(), text))
                save_todos(data)
                message = f"Added note to task {task.task_id}"
            else:
                message = "Note cancelled"
        elif key == "x" and tasks:
            task = tasks[selected]
            answer = tui_prompt(
                screen, curses_module, f"Delete task {task.task_id}? [y/N] "
            )
            if answer.casefold() in {"y", "yes"}:
                data.tasks.remove(task)
                save_todos(data)
                message = f"Deleted task {task.task_id}"
            else:
                message = "Delete cancelled"
        elif key == "e" and tasks:
            task = tasks[selected]
            description = tui_prompt(
                screen,
                curses_module,
                f"New description for {task.task_id} (blank cancels): ",
            )
            if description:
                task.description = description
                save_todos(data)
                message = f"Edited task {task.task_id}"
            else:
                message = "Edit cancelled"
        elif key == "/":
            query = tui_prompt(
                screen, curses_module, "Search (blank clears): "
            )
            selected = 0
            scroll_offset = 0
            message = "Search updated"
        elif key == "t":
            view = "today"
            selected = 0
            scroll_offset = 0
            message = "Showing today's tasks"
        elif key == "p":
            view = "pending"
            selected = 0
            scroll_offset = 0
            message = "Showing today's pending tasks"
        elif key == "A":
            view = "all"
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
        "delete", aliases=["x"], help="delete a task"
    )
    delete_parser.add_argument("task_id", type=parse_task_id, metavar="TASK-ID")
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
