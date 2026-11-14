"""Concurrent callers obtain unique machine-readable references before scheduling completes."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io
import json
import tempfile
import unittest
from podvigil.cli import run_cli
from podvigil.modal_backend import runner
from tests.support.modal import spec


class DetachedJson(unittest.TestCase):
    def test_each_released_job_returns_exactly_one_json_line_without_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            references = []
            root = Path(directory)
            config = {**spec(), "inputs": {}}
            (root / "job.json").write_text(json.dumps(config))
            for _ in range(3):
                out, err = io.StringIO(), io.StringIO()
                with (
                    patch.object(runner, "credential_proof", return_value={"modal": "dummy"}),
                    patch.object(runner, "spawn", return_value=SimpleNamespace(close=lambda: None)),
                    patch.object(
                        runner, "wait", side_effect=AssertionError("Must not wait for capacity")
                    ),
                    redirect_stdout(out),
                    redirect_stderr(err),
                ):
                    self.assertEqual(
                        run_cli(
                            [
                                "--state-dir",
                                str(root / "state"),
                                "launch",
                                str(root / "job.json"),
                                "--detach",
                            ]
                        ),
                        9,
                    )
                self.assertEqual(len(out.getvalue().splitlines()), 1)
                document = json.loads(out.getvalue())
                references.append(document["job_id"])
                self.assertTrue((Path(document["ledger"]) / "release.json").exists())
                self.assertEqual(document["sandbox"]["state"], "not_created")
            self.assertEqual(len(set(references)), 3)
