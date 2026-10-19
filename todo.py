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
from dataclasses import dataclass
from datetime import date
from pathlib import Path


TODO_FILE = Path(__file__).resolve().parent / "todo.txt"
FORMAT_LINE = "# todo-cli v1 next_id={next_id}"
COLUMN_LINE = "# id\tstatus\tcreated\tcompleted\tdescription"
FORMAT_PATTERN = re.compile(r"# todo-cli v1 next_id=([1-9][0-9]*)")


class TodoFileError(Exception):
    """Raised when todo.txt cannot be safely read."""


@dataclass
class Task:
    task_id: int
    status: str
    created: date
    completed: date | None
    description: str


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


def parse_task_row(row: list[str], line_number: int) -> Task:
    """Validate and convert one tab-separated task row."""
    if len(row) != 5:
        raise TodoFileError(
            f"line {line_number}: expected 5 tab-separated fields, found {len(row)}"
        )

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
    next_id = int(format_match.group(1))

    if len(lines) < 2 or lines[1].rstrip("\r\n") != COLUMN_LINE:
        raise TodoFileError("line 2: missing or invalid column header")

    tasks: list[Task] = []
    seen_ids: set[int] = set()
    for line_number, line in enumerate(lines[2:], start=3):
        if not line.strip():
            continue
        try:
            row = next(csv.reader([line], delimiter="\t"))
        except csv.Error as exc:
            raise TodoFileError(f"line {line_number}: invalid tab-separated data") from exc
        task = parse_task_row(row, line_number)
        if task.task_id in seen_ids:
            raise TodoFileError(
                f"line {line_number}: duplicate task ID {task.task_id}"
            )
        seen_ids.add(task.task_id)
        tasks.append(task)

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
                        task.task_id,
                        task.status,
                        task.created.isoformat(),
                        task.completed.isoformat() if task.completed else "-",
                        task.description,
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


def command_add(args: argparse.Namespace) -> int:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo",
        description="Track daily tasks in a local, Vim-friendly todo.txt file.",
    )
    subparsers = parser.add_subparsers(dest="command")

    add_parser = subparsers.add_parser("add", help="add a new task")
    add_parser.add_argument("description", help="task description")
    add_parser.set_defaults(handler=command_add)

    list_parser = subparsers.add_parser("list", help="list tasks")
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

    delete_parser = subparsers.add_parser("delete", help="delete a task")
    delete_parser.add_argument("task_id", type=parse_task_id, metavar="TASK-ID")
    delete_parser.set_defaults(handler=command_delete)

    edit_parser = subparsers.add_parser("edit", help="open todo.txt in an editor")
    edit_parser.set_defaults(handler=command_edit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    try:
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
