"""Local file mistakes are actionable diagnostics, not cloud lifecycle failures."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
import io
import json
import tempfile
import unittest

from podvigil.cli import run_cli


class LocalJobErrors(unittest.TestCase):
    def test_plan_names_missing_file_and_json_error_position_without_cloud_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.json").write_text('{\n "provider": }', encoding="utf-8")
            (root / "encoding.json").write_bytes(b"\xff")
            for name, expected in (
                ("missing.json", "Cannot read job file"),
                ("broken.json", "line 2, column 14"),
                ("encoding.json", "not valid UTF-8"),
            ):
                out, err = io.StringIO(), io.StringIO()
                with (
                    patch(
                        "podvigil.modal_backend.provider.sdk", side_effect=AssertionError("offline")
                    ),
                    redirect_stdout(out),
                    redirect_stderr(err),
                ):
                    self.assertEqual(
                        run_cli(
                            ["--state-dir", str(root / "absent-state"), "plan", str(root / name)]
                        ),
                        1,
                    )
                message = json.loads(out.getvalue())["error"]
                self.assertIn(name, message)
                self.assertIn(expected, message)
                self.assertNotIn("Modal operation failed", message)
                self.assertIn(message, err.getvalue())
            self.assertFalse((root / "absent-state").exists())
