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

    def test_no_command_shows_help(self) -> None:
        result = self.run_todo()
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: todo", result.stdout)
        self.assertIn("add", result.stdout)
        self.assertIn("list", result.stdout)

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
