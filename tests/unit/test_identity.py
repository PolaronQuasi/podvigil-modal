"""Source/distribution identity and version output require neither credentials nor Modal."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
import io
import json
import tempfile
import unittest

from podvigil.cli import run_cli
from podvigil.identity import identity


class Identity(unittest.TestCase):
    def test_current_checkout_has_commit_and_source_fingerprint(self):
        result = identity()
        # Network-free CI source copies may have no .git; fingerprints always identify their bytes.
        self.assertEqual(len(result["source_sha256"]), 64)
        if result["commit"]:
            self.assertRegex(result["commit"], r"^[0-9a-f]{40,64}$")
        self.assertEqual(result["version"], "0.1.0")

    def test_archive_preserves_stamped_commit_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src/podvigil"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("# test")
            (package / "_build_info.json").write_text(
                json.dumps({"commit": "a" * 40, "dirty": False})
            )
            with patch("subprocess.run", side_effect=AssertionError("No git for archived source")):
                self.assertEqual(identity(root)["commit"], "a" * 40)

    def test_version_command_is_available_without_sdk(self):
        out = io.StringIO()
        with (
            patch("podvigil.modal_backend.provider.sdk", side_effect=AssertionError("offline")),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(run_cli(["--version"]), 0)
        self.assertIn("podvigil-modal 0.1.0 (commit", out.getvalue())
