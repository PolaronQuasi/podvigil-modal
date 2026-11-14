"""Non-secret child environment validation does not expose host credentials."""

from pathlib import Path
import json
import tempfile
import unittest

from podvigil.modal_backend.spec import read_spec
from tests.support.modal import spec


class WorkloadEnvironment(unittest.TestCase):
    def test_valid_map_survives_validation_and_omission_keeps_legacy_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            config = spec()
            config["inputs"] = {}
            path.write_text(json.dumps(config))
            self.assertNotIn("workload_env", read_spec(path)[0])
            config["workload_env"] = {"OMP_NUM_THREADS": "4", "EMPTY": "", "LABEL": "demo"}
            path.write_text(json.dumps(config))
            self.assertEqual(read_spec(path)[0]["workload_env"], config["workload_env"])

    def test_rejects_invalid_names_values_reserved_names_and_oversize_maps(self):
        invalid = [
            [],
            None,
            {"BAD-NAME": "x"},
            {"1NAME": "x"},
            {"": "x"},
            {"PODVIGIL_CPU": "99"},
            {"modal_token_secret": "dummy"},
            {"COUNT": 4},
            {"NUL": "a\0b"},
            {"LONG": "x" * 8193},
            {"X" * 129: "x"},
            {"N" + str(i): "x" for i in range(129)},
            {"N" + str(i): "x" * 8192 for i in range(9)},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            for value in invalid:
                config = dict(spec(), inputs={}, workload_env=value)
                path.write_text(json.dumps(config))
                with (
                    self.subTest(value=type(value).__name__),
                    self.assertRaisesRegex(ValueError, "workload_env"),
                ):
                    read_spec(path)
