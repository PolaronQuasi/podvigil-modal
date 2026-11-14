"""Keep the Modal-only CLI, distribution boundary and operation codes aligned."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import io
import re
import unittest

from podvigil.cli import run_cli
from podvigil.outcomes import EXIT_CODES


class Contract(unittest.TestCase):
    def test_operations_documents_every_outcome_without_reusing_reserved_codes(self):
        root = Path(__file__).resolve().parents[2]
        operations = (root / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        self.assertEqual(len(EXIT_CODES), len(set(EXIT_CODES.values())))
        self.assertFalse({3, 5, 12} & set(EXIT_CODES.values()))
        self.assertEqual(EXIT_CODES["no_capacity"], 4)
        documented = {
            name: int(code)
            for name, code in re.findall(r"\|\s*(\w+)\s*\|\s*(\d+)\s*\|", operations)
        }
        self.assertEqual(documented, EXIT_CODES)

    def test_cli_exposes_only_modal_commands(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run_cli(["--help"]), 0)
        self.assertIn("{list,plan,launch,status,wait,cancel,recover,logs}", out.getvalue())
        for command in ("survey", "run-local", "watchdog", "export-unresolved"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(run_cli([command]), 64)

    def test_distribution_has_only_the_modal_dependency_and_executable(self):
        root = Path(__file__).resolve().parents[2]
        config = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "podvigil-modal"', config)
        self.assertIn('"modal==1.6.4"', config)
        self.assertIn('podvigil-modal = "podvigil.cli:run_cli"', config)
        for name in ("provider", "controller", "podrt"):
            self.assertFalse((root / "src/podvigil" / name / "__init__.py").exists())
