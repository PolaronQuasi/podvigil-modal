"""Publication checks reject credential data without printing the rejected values."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
import io
import tempfile
import unittest

from tools import ci, check_docs


class PublicArtifacts(unittest.TestCase):
    def test_nested_runner_guides_are_checked_without_reading_untracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / ".gitea/runner/README.md"
            guide.parent.mkdir(parents=True)
            guide.write_text("[Missing](absent.md)", encoding="utf-8")
            (root / "private.md").write_text("[Private](also-missing.md)", encoding="utf-8")
            with (
                patch.object(check_docs, "ROOT", root),
                patch.object(
                    check_docs.subprocess, "check_output", return_value=b".gitea/runner/README.md\0"
                ),
                self.assertRaisesRegex(RuntimeError, "absent.md") as error,
            ):
                check_docs.main()
            self.assertNotIn("also-missing", str(error.exception))

    def test_known_tokens_in_documentation_fail_without_disclosing_values(self):
        values = [
            prefix + "A" * count
            for prefix, count in (
                ("ak-", 24),
                ("as-", 24),
                ("rpa_", 40),
                ("rps_", 40),
                ("rp_", 40),
                ("AKIA", 16),
                ("ASIA", 16),
                ("ghp_", 40),
                ("github_pat_", 40),
                ("sk-", 40),
                ("hf_", 40),
            )
        ]
        values.append("-----BEGIN " + "RSA PRIVATE KEY-----")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in values:
                with self.subTest(prefix=value[:4]):
                    (root / "guide.md").write_text("Configuration: " + value, encoding="utf-8")
                    output = io.StringIO()
                    with (
                        patch.object(ci, "ROOT", root),
                        patch.object(ci, "tracked", return_value=["guide.md"]),
                        patch.object(ci.subprocess, "run") as command,
                        redirect_stdout(output),
                        redirect_stderr(output),
                        self.assertRaisesRegex(RuntimeError, "guide.md") as error,
                    ):
                        ci.audit()
                    self.assertNotIn(value, str(error.exception) + output.getvalue())
                    command.assert_not_called()

    def test_private_files_are_refused_even_when_forced_into_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                ".env",
                ".env.local",
                ".modal.toml",
                "credentials.env",
                ".runner",
                ".aws/credentials",
                ".ssh/id_ed25519",
                "cert.p12",
                "cert.PFX",
            ):
                with self.subTest(name=name), patch.object(ci, "ROOT", root):
                    with patch.object(ci, "tracked", return_value=[name]):
                        with self.assertRaisesRegex(RuntimeError, "Private file"):
                            ci.audit()

    def test_documented_variable_names_and_dummy_values_are_allowed(self):
        content = (
            "MODAL_TOKEN_ID and MODAL_TOKEN_SECRET belong in the local profile.\n"
            "PODVIGIL_MODAL_STATE_DIR=./output/modal-state\n"
            "ak-test-only as-test-only OMP_NUM_THREADS=2\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "guide.md").write_text(content, encoding="utf-8")
            with (
                patch.object(ci, "ROOT", root),
                patch.object(ci, "tracked", return_value=["guide.md"]),
                patch.object(ci.subprocess, "run") as command,
                redirect_stdout(io.StringIO()),
            ):
                ci.audit()
                command.assert_called_once()
