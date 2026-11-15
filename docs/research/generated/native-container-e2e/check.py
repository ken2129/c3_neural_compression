"""Tiny single-GPU correctness check; run with /usr/local/bin/python."""
import os

# Set before importing JAX; never use a CPU fallback or preallocate GPU memory.
os.environ["CUDA_VISIBLE_DEVICES"] = "0"
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"
os.environ["JAX_PLATFORMS"] = "cuda"

import json
from pathlib import Path
import platform
import time

import jax
import jax.numpy as jnp
import jaxlib
import numpy as np


def main():
    devices = jax.devices()
    assert jax.default_backend() == "gpu", "CPU fallback is a failed GPU check"
    assert len(devices) == 1 and devices[0].platform == "gpu", devices
    device = devices[0]
    rng = np.random.default_rng(113)
    a = rng.uniform(-1, 1, (32, 32)).astype(np.float32)
    b = rng.uniform(-1, 1, (32, 32)).astype(np.float32)
    # Independent CPU NumPy reference with float64 accumulation.
    reference = a.astype(np.float64) @ b.astype(np.float64)
    x, y = jax.device_put(a, device), jax.device_put(b, device)
    x.block_until_ready()
    y.block_until_ready()
    start = time.perf_counter()
    product = jnp.matmul(x, y, precision=jax.lax.Precision.HIGHEST)
    product.block_until_ready()
    elapsed = time.perf_counter() - start
    assert product.devices() == {device}
    actual = np.asarray(product)
    error = float(np.max(np.abs(actual.astype(np.float64) - reference)))
    passed = bool(np.isfinite(actual).all() and error <= 1e-4)
    result = {
        "passed": passed,
        "backend": jax.default_backend(),
        "device": {"platform": device.platform, "kind": device.device_kind,
                   "logical_id": device.id},
        "gpu_count": len(devices),
        "matrix_shapes": {"a": list(a.shape), "b": list(b.shape)},
        "input_dtype": "float32", "reference_dtype": "float64",
        "seed": 113, "absolute_tolerance": 1e-4,
        "max_absolute_error": error,
        "elapsed_calculation_seconds": elapsed,
        "timing_scope": "first matmul dispatch through block_until_ready; includes compilation; excludes input transfer and CPU reference",
        "versions": {"python": platform.python_version(), "jax": jax.__version__,
                     "jaxlib": jaxlib.__version__, "numpy": np.__version__},
        "preallocation": False,
    }
    Path(__file__).with_name("result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if not passed:
        raise SystemExit("GPU result exceeds absolute tolerance or is nonfinite")


if __name__ == "__main__":
    main()
