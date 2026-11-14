"""Pin the native Sandbox GPU-list refusal; never emulate it with create retries."""

import inspect
import unittest

from podvigil.modal_backend.provider import sdk


class NativeGpuFallback(unittest.TestCase):
    def test_pinned_sdk_rejects_gpu_lists_before_any_network_call(self):
        try:
            modal = sdk()
        except RuntimeError:
            self.skipTest("Pinned SDK contract lane only")
        from modal.sandbox._common import SandboxCreateArgs, SandboxCreateCommon

        inspect.signature(modal.Sandbox.create).bind("python3", "-c", "pass", gpu="T4", timeout=60)
        with self.assertRaisesRegex(modal.exception.InvalidError, "do not support.*list"):
            SandboxCreateCommon(
                SandboxCreateArgs(args=("python3", "-c", "pass"), gpu=["L4", "T4"]), secrets=[]
            )
