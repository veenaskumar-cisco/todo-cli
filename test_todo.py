"""Integration tests for the todo command-line program."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import todo


PROJECT_DIR = Path(__file__).resolve().parent


class TodoCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name)
        self.script = self.project / "todo.py"
        shutil.copy2(PROJECT_DIR / "todo.py", self.script)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_todo(
        self,
        *arguments: str,
        input_text: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.script), *arguments],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def test_no_command_prompts_for_todays_tasks(self) -> None:
        today = date.today().isoformat()
        result = self.run_todo(
            input_text="Run BGP sanity test\nCheck SSH login\n\n"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"Today's todos ({today})", result.stdout)
        self.assertIn("1st todo:", result.stdout)
        self.assertIn("2nd todo:", result.stdout)
        self.assertIn(f"Added tasks 1, 2 (created {today})", result.stdout)

        listed = self.run_todo("list")
        self.assertIn("Run BGP sanity test", listed.stdout)
        self.assertIn("Check SSH login", listed.stdout)

    def test_help_flag_still_shows_help(self) -> None:
        result = self.run_todo("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: todo", result.stdout)

    def test_short_command_aliases(self) -> None:
        added = self.run_todo("a", "Alias task")
        self.assertEqual(added.returncode, 0)
        self.assertIn("Added task 1", added.stdout)

        listed = self.run_todo("ls")
        self.assertEqual(listed.returncode, 0)
        self.assertIn("Alias task", listed.stdout)

        deleted = self.run_todo("x", "1", input_text="y\n")
        self.assertEqual(deleted.returncode, 0)
        self.assertIn("Deleted task 1", deleted.stdout)

        fake_editor = self.project / "fake_editor.py"
        fake_editor.write_text("", encoding="utf-8")
        environment = {
            **os.environ,
            "EDITOR": f"{sys.executable} {fake_editor}",
        }
        edited = self.run_todo("e", environment=environment)
        self.assertEqual(edited.returncode, 0)

    def test_add_alias_without_description_starts_prompt(self) -> None:
        result = self.run_todo("a", input_text="Prompted task\n\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("1st todo:", result.stdout)

        listed = self.run_todo("ls")
        self.assertIn("Prompted task", listed.stdout)

    def test_tui_requires_an_interactive_terminal(self) -> None:
        result = self.run_todo("t")
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires an interactive terminal", result.stderr)

    def test_tui_fuzzy_search(self) -> None:
        self.assertTrue(todo.fuzzy_match("bgpst", "Run BGP sanity test"))
        self.assertTrue(todo.fuzzy_match("ssh", "Check remote-leaf SSH login"))
        self.assertFalse(todo.fuzzy_match("status", "Study Linux basics"))

    def test_tui_renders_and_quits(self) -> None:
        class FakeCurses:
            A_BOLD = 1
            A_REVERSE = 2
            A_NORMAL = 0
            KEY_DOWN = 258
            KEY_UP = 259
            error = RuntimeError

            @staticmethod
            def curs_set(_visibility: int) -> None:
                return None

        class FakeScreen:
            def __init__(self) -> None:
                self.keys = iter(["u", "q"])

            def getmaxyx(self) -> tuple[int, int]:
                return (24, 100)

            def keypad(self, _enabled: bool) -> None:
                return None

            def erase(self) -> None:
                return None

            def addnstr(
                self,
                _row: int,
                _column: int,
                _text: str,
                _length: int,
                _attributes: int = 0,
            ) -> None:
                return None

            def refresh(self) -> None:
                return None

            def get_wch(self) -> str:
                return next(self.keys)

        task = todo.Task(1, "done", date.today(), date.today(), "Test task")
        data = todo.TodoData(next_id=2, tasks=[task])
        with (
            mock.patch.object(todo, "load_todos", return_value=data),
            mock.patch.object(todo, "save_todos") as save_todos,
        ):
            result = todo.run_tui(FakeScreen(), FakeCurses())
        self.assertEqual(result, 0)
        self.assertEqual(task.status, "pending")
        self.assertIsNone(task.completed)
        save_todos.assert_called_once_with(data)

    def test_add_list_and_complete_task(self) -> None:
        today = date.today().isoformat()

        added = self.run_todo("add", "Run BGP transit sanity")
        self.assertEqual(added.returncode, 0)
        self.assertIn(f"Added task 1 (created {today})", added.stdout)

        listed = self.run_todo("list")
        self.assertEqual(listed.returncode, 0)
        self.assertIn("1      pending", listed.stdout)
        self.assertIn("Run BGP transit sanity", listed.stdout)

        completed = self.run_todo("done", "1")
        self.assertEqual(completed.returncode, 0)
        self.assertIn(f"Completed task 1 on {today}", completed.stdout)

        pending = self.run_todo("list", "--pending")
        self.assertEqual(pending.returncode, 0)
        self.assertIn(f"No pending tasks found for {today}", pending.stdout)

        all_tasks = self.run_todo("list", "--all")
        self.assertIn("1      done", all_tasks.stdout)

        reopened = self.run_todo("u", "1")
        self.assertEqual(reopened.returncode, 0)
        self.assertIn("Reopened task 1", reopened.stdout)

        pending_again = self.run_todo("list", "--pending")
        self.assertIn("1      pending", pending_again.stdout)
        self.assertIn("-            Run BGP transit sanity", pending_again.stdout)

        already_pending = self.run_todo("undo", "1")
        self.assertEqual(already_pending.returncode, 1)
        self.assertIn("already pending", already_pending.stderr)

    def test_ids_are_not_reused_after_delete(self) -> None:
        self.run_todo("add", "First")
        deleted = self.run_todo("delete", "1", input_text="yes\n")
        self.assertEqual(deleted.returncode, 0)

        added = self.run_todo("add", "Second")
        self.assertIn("Added task 2", added.stdout)

    def test_invalid_inputs_are_clear(self) -> None:
        invalid_date = self.run_todo("list", "--date", "2026-02-30")
        self.assertEqual(invalid_date.returncode, 2)
        self.assertIn("use YYYY-MM-DD", invalid_date.stderr)

        invalid_id = self.run_todo("done", "abc")
        self.assertEqual(invalid_id.returncode, 2)
        self.assertIn("task ID must be a positive integer", invalid_id.stderr)

        missing_id = self.run_todo("done", "99")
        self.assertEqual(missing_id.returncode, 1)
        self.assertIn("task ID 99 does not exist", missing_id.stderr)

    def test_malformed_file_is_not_overwritten(self) -> None:
        todo_file = self.project / "todo.txt"
        original = "this is not a valid todo file\n"
        todo_file.write_text(original, encoding="utf-8")

        result = self.run_todo("add", "Must not overwrite")
        self.assertEqual(result.returncode, 1)
        self.assertIn("todo.txt was not changed", result.stderr)
        self.assertEqual(todo_file.read_text(encoding="utf-8"), original)

    def test_edit_opens_a_malformed_file_without_overwriting_it(self) -> None:
        todo_file = self.project / "todo.txt"
        original = "repair this malformed content\n"
        todo_file.write_text(original, encoding="utf-8")
        fake_editor = self.project / "fake_editor.py"
        fake_editor.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "Path(sys.argv[1] + '.opened').write_text('yes', encoding='utf-8')\n",
            encoding="utf-8",
        )
        environment = {
            **os.environ,
            "EDITOR": f"{sys.executable} {fake_editor}",
        }

        result = self.run_todo("edit", environment=environment)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(Path(f"{todo_file}.opened").exists())
        self.assertIn("edited todo.txt is malformed", result.stderr)
        self.assertEqual(todo_file.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
