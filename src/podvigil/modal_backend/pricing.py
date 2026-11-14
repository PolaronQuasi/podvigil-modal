"""Dated sanity check, never a live quote, spending cap or invoice.

Verified 2026-11-14 at https://modal.com/pricing. Sandbox CPU/memory use the published
non-preemptible rates; the Sandbox GPU section explicitly links to standard GPU pricing.
"""

AS_OF = "2026-11-14"
SOURCE = "https://modal.com/pricing"
CPU_SECOND = 0.00003942
GIB_SECOND = 0.00000667
GPU_SECOND = {
    "T4": 0.000164,
    "L4": 0.000222,
    "A10": 0.000306,
    "L40S": 0.000542,
    "A100-40GB": 0.000583,
    "A100-80GB": 0.000694,
    "RTX-PRO-6000": 0.000842,
    "H100": 0.001097,
    "H200": 0.001261,
    "B200": 0.001736,
    "B300": 0.001972,
}


def hourly_estimate(gpu, cpu, memory_mib):
    gpu_second = 0
    if gpu:
        name, _, count = gpu.partition(":")
        gpu_second = GPU_SECOND[name] * (int(count) if count else 1)
    return round(3600 * (gpu_second + cpu * CPU_SECOND + memory_mib / 1024 * GIB_SECOND), 9)


def price_check(spec):
    calculated = hourly_estimate(spec.get("gpu"), spec["cpu"], spec["memory_mib"])
    low = spec["estimated_hourly_usd"] < calculated * 0.9 - 1e-12
    return {
        "as_of": AS_OF,
        "source": SOURCE,
        "calculated_hourly_usd": calculated,
        "user_hourly_usd": spec["estimated_hourly_usd"],
        "warning": "The user estimate is more than 10% below the dated resource table; verify current pricing."
        if low
        else None,
        "basis": "Sandbox CPU and memory rates; standard GPU rates. User quote remains authoritative.",
    }


def spend_estimate(job):
    timing = job.get("runtime_timing") or {}
    seconds = timing.get("supervisor_seconds")
    return {
        "sandbox_runtime_seconds": seconds,
        "estimated_spend_usd": round(seconds * job["config"]["estimated_hourly_usd"] / 3600, 9)
        if seconds is not None
        else None,
        "hourly_usd": job["config"]["estimated_hourly_usd"],
        "duration_basis": "Supervisor entry through manifest preparation; excludes boot and final commit."
        if seconds is not None
        else "No complete supervisor timing receipt available.",
        "label": "Estimate, not an invoice; excludes unobserved runtime, storage, builds and egress.",
    }
