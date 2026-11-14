"""Small lifecycle/GPU visibility check, not a training benchmark."""

from pathlib import Path
import argparse
import ctypes
import json
import os

parser = argparse.ArgumentParser()
parser.add_argument("--gpu", action="store_true")
args = parser.parse_args()
result = {"workload": "modal-smoke", "ok": True}
if args.gpu:
    cuda = ctypes.CDLL("libcuda.so.1")
    if cuda.cuInit(0) != 0:
        raise RuntimeError("CUDA driver initialization failed")
    count = ctypes.c_int()
    if cuda.cuDeviceGetCount(ctypes.byref(count)) != 0 or count.value < 1:
        raise RuntimeError("No CUDA device visible")
    device = ctypes.c_int()
    name = ctypes.create_string_buffer(128)
    if (
        cuda.cuDeviceGet(ctypes.byref(device), 0) != 0
        or cuda.cuDeviceGetName(name, 128, device) != 0
    ):
        raise RuntimeError("Cannot identify CUDA device")
    result.update(gpu_count=count.value, gpu_name=name.value.decode())
output = Path(os.environ["PODVIGIL_OUTPUT_DIR"])
(output / "result.json").write_text(json.dumps(result) + "\n", encoding="utf-8")
print(json.dumps(result))
