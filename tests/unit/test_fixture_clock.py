"""A dummy quote must not look future-dated to a lower-resolution worker clock."""

from datetime import datetime, timezone
from unittest.mock import patch
import unittest

from podvigil.modal_backend.spec import check_quote
from tests.support import modal


class FixtureClock(unittest.TestCase):
    def test_quote_tolerates_rounding_between_fixture_and_worker_clocks(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(modal, "datetime") as clock:
            clock.now.return_value = now
            config = modal.spec()
        check_quote(config, now.timestamp() - 0.000001)
        config["rate_checked_utc"] = now.isoformat()
        with self.assertRaises(ValueError):
            check_quote(config, now.timestamp() - 0.000001)
