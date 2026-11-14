"""Detect common mojibake without rejecting legitimate Unicode or scanning private files."""

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import tempfile
import unittest

from tools import check_docs


class TextEncoding(unittest.TestCase):
    def test_common_corruption_and_invalid_utf8_are_reported_with_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = "30\u00e2\u20ac\u201c86400"
            (root / "guide.md").write_text("valid\n" + bad, encoding="utf-8")
            (root / "broken.py").write_bytes(b"\xff")
            (root / "replacement.txt").write_text("\ufffd", encoding="utf-8")
            (root / ".prettierignore").write_text(bad, encoding="utf-8")
            errors = check_docs.text_errors(
                root, ["guide.md", "broken.py", "replacement.txt", ".prettierignore"]
            )
            self.assertEqual(len(errors), 4)
            self.assertIn("guide.md:2", errors[0])
            self.assertIn("invalid UTF-8", errors[1])

    def test_legitimate_unicode_passes_and_main_uses_tracked_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "guide.md").write_text("30–86400; Résumé; Ελληνικά; 日本語", encoding="utf-8")
            (root / "untracked.txt").write_text("\ufffd", encoding="utf-8")
            with (
                patch.object(check_docs, "ROOT", root),
                patch.object(
                    check_docs.subprocess, "check_output", return_value=b"guide.md\0"
                ) as inventory,
                redirect_stdout(io.StringIO()),
            ):
                check_docs.main()
                inventory.assert_called_once_with(["git", "ls-files", "-z"], cwd=root)
            (root / "guide.md").write_text("\u00e2\u20ac\u201c", encoding="utf-8")
            with (
                patch.object(check_docs, "ROOT", root),
                patch.object(check_docs.subprocess, "check_output", return_value=b"guide.md\0"),
                self.assertRaisesRegex(RuntimeError, "corrupted text"),
            ):
                check_docs.main()
