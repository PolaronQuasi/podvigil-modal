"""Dated price arithmetic is advisory and cannot silently replace a user's quote."""

import unittest
from podvigil.modal_backend.pricing import hourly_estimate, price_check, spend_estimate
from tests.support.modal import spec


class Pricing(unittest.TestCase):
    def test_sandbox_cpu_memory_and_standard_gpu_rates(self):
        self.assertAlmostEqual(hourly_estimate(None, 1, 1024), 0.165924)
        self.assertAlmostEqual(hourly_estimate("T4", 1, 1024), 0.756324)
        self.assertAlmostEqual(
            hourly_estimate("L4:2", 4, 8192),
            (2 * 0.000222 + 4 * 0.00003942 + 8 * 0.00000667) * 3600,
        )

    def test_warns_only_when_more_than_ten_percent_low(self):
        config = spec()
        calculated = hourly_estimate(config["gpu"], config["cpu"], config["memory_mib"])
        config["estimated_hourly_usd"] = calculated * 0.9
        self.assertIsNone(price_check(config)["warning"])
        config["estimated_hourly_usd"] = calculated * 0.899
        self.assertIsNotNone(price_check(config)["warning"])
        self.assertEqual(price_check(config)["user_hourly_usd"], config["estimated_hourly_usd"])

    def test_spend_uses_measured_receipt_and_users_rate(self):
        record = {"config": spec(), "runtime_timing": {"supervisor_seconds": 90}}
        self.assertEqual(spend_estimate(record)["estimated_spend_usd"], 0.025)
        record.pop("runtime_timing")
        self.assertIsNone(spend_estimate(record)["estimated_spend_usd"])
        self.assertIn("not an invoice", spend_estimate(record)["label"])
