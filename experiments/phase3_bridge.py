"""Runs the Phase 3 Gate A JAX/PyTorch bridge on one image."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import os
from pathlib import Path
import platform
import time

import numpy as np
from PIL import Image


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--image", type=Path, required=True)
  parser.add_argument("--output-dir", type=Path, required=True)
  parser.add_argument("--device", default="cuda")
  parser.add_argument("--feature-layer", default="0")
  parser.add_argument("--perturbation", type=float, default=1.0 / 255.0)
  parser.add_argument("--directional-epsilon", type=float, default=3e-3)
  parser.add_argument("--overwrite", action="store_true")
  return parser


def _atomic_json(path: Path, value) -> None:
  temporary = path.with_suffix(path.suffix + ".tmp")
  with temporary.open("w", encoding="utf-8") as stream:
    json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
  os.replace(temporary, path)


def _torch_memory(torch, label: str) -> dict:
  if not torch.cuda.is_available():
    return {"label": label, "cuda": False}
  torch.cuda.synchronize()
  return {
      "label": label,
      "cuda": True,
      "allocated_bytes": torch.cuda.memory_allocated(),
      "reserved_bytes": torch.cuda.memory_reserved(),
      "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
      "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
  }


def _load_rgb(path: Path) -> np.ndarray:
  with Image.open(path) as image:
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
  if rgb.ndim != 3 or rgb.shape[-1] != 3:
    raise ValueError(f"Expected an RGB image, got {rgb.shape}")
  return rgb


def main(argv=None) -> int:
  args = _parser().parse_args(argv)
  if args.perturbation <= 0:
    raise ValueError("--perturbation must be positive")
  if args.directional_epsilon <= 0:
    raise ValueError("--directional-epsilon must be positive")
  metrics_path = args.output_dir / "metrics.json"
  if metrics_path.exists() and not args.overwrite:
    raise FileExistsError(f"Refusing to overwrite without --overwrite: {metrics_path}")

  # This must be set before JAX initializes a CUDA backend.
  os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
  os.environ.setdefault("TORCH_HOME", "/workspace/datasets/torch")

  import jax  # pylint: disable=g-import-not-at-top
  import jax.numpy as jnp  # pylint: disable=g-import-not-at-top
  import torch  # pylint: disable=g-import-not-at-top
  import torchvision  # pylint: disable=g-import-not-at-top

  from c3_neural_compression.machine_loss import faster_rcnn  # pylint: disable=g-import-not-at-top
  from c3_neural_compression.machine_loss import torch_bridge  # pylint: disable=g-import-not-at-top

  if args.device.startswith("cuda") and not torch.cuda.is_available():
    raise RuntimeError(f"CUDA requested ({args.device}) but PyTorch cannot see a GPU")
  if args.device.startswith("cuda"):
    jax_device = jax.devices("gpu")[0]
  else:
    jax_device = jax.devices("cpu")[0]

  args.output_dir.mkdir(parents=True, exist_ok=True)
  image = _load_rgb(args.image)
  memory = [_torch_memory(torch, "before_detector")]
  feature_loss, weights = faster_rcnn.create_frozen_fpn_loss(
      device=args.device, layer=args.feature_layer
  )
  memory.append(_torch_memory(torch, "after_detector"))

  reference = torch.from_numpy(image.copy()).to(args.device)
  metadata = feature_loss.cache_reference(reference)
  del reference
  memory.append(_torch_memory(torch, "after_reference_cache"))
  checksum_before = faster_rcnn.detector_parameter_checksum(feature_loss.model)

  jax_loss = torch_bridge.make_jax_vjp_loss(feature_loss)
  original = jax.device_put(image, jax_device)
  same_loss = jax_loss(original)
  same_loss.block_until_ready()

  phase = np.arange(image.size, dtype=np.float32).reshape(image.shape)
  perturbed_np = np.clip(
      image + args.perturbation * np.sin(phase * np.float32(0.017)), 0.0, 1.0
  )
  perturbed = jax.device_put(perturbed_np, jax_device)
  if torch.cuda.is_available():
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
  start = time.perf_counter()
  perturbed_loss, image_gradient = jax.value_and_grad(jax_loss)(perturbed)
  jax.block_until_ready((perturbed_loss, image_gradient))
  if torch.cuda.is_available():
    torch.cuda.synchronize()
  elapsed = time.perf_counter() - start
  memory.append(_torch_memory(torch, "after_backward"))

  gradient_norm = float(jnp.linalg.norm(image_gradient))
  gradient_finite = bool(jnp.all(jnp.isfinite(image_gradient)))
  direction = image_gradient / jnp.linalg.norm(image_gradient)
  finite_difference_epsilon = args.directional_epsilon
  loss_plus = jax_loss(perturbed + finite_difference_epsilon * direction)
  loss_minus = jax_loss(perturbed - finite_difference_epsilon * direction)
  jax.block_until_ready((loss_plus, loss_minus))
  directional_finite_difference = float(
      (loss_plus - loss_minus) / (2 * finite_difference_epsilon)
  )
  directional_vjp = float(jnp.vdot(image_gradient, direction))
  directional_relative_error = abs(
      directional_finite_difference - directional_vjp
  ) / max(abs(directional_vjp), 1e-12)
  directional_sign_consistent = (
      directional_finite_difference * directional_vjp > 0
  )
  checksum_after = faster_rcnn.detector_parameter_checksum(feature_loss.model)
  detector_gradients_none = feature_loss.detector_gradients_are_none()
  same_loss_value = float(same_loss)
  perturbed_loss_value = float(perturbed_loss)

  if not math.isfinite(perturbed_loss_value):
    raise RuntimeError("Feature loss is not finite")
  if not gradient_finite or gradient_norm <= 0:
    raise RuntimeError("Image gradient must be finite and non-zero")
  if not directional_sign_consistent:
    raise RuntimeError("Directional finite difference disagrees with the VJP")
  if checksum_before != checksum_after or not detector_gradients_none:
    raise RuntimeError("Frozen detector state changed or accumulated gradients")

  result = {
      "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
      "gate": "A_non_jit",
      "image": str(args.image.resolve()),
      "image_shape_hwc": list(image.shape),
      "feature": dataclasses.asdict(metadata),
      "weights": str(weights),
      "weights_url": weights.url,
      "same_image_loss": same_loss_value,
      "perturbed_image_loss": perturbed_loss_value,
      "perturbation": args.perturbation,
      "image_gradient_norm": gradient_norm,
      "image_gradient_finite": gradient_finite,
      "directional_finite_difference": directional_finite_difference,
      "directional_vjp": directional_vjp,
      "directional_relative_error": directional_relative_error,
      "directional_sign_consistent": directional_sign_consistent,
      "directional_epsilon": finite_difference_epsilon,
      "detector_gradients_none": detector_gradients_none,
      "detector_checksum_before": checksum_before,
      "detector_checksum_after": checksum_after,
      "forward_backward_seconds": elapsed,
      "memory": memory,
      "environment": {
          "python": platform.python_version(),
          "platform": platform.platform(),
          "jax": jax.__version__,
          "jax_device": str(jax_device),
          "torch": torch.__version__,
          "torchvision": torchvision.__version__,
          "torch_cuda": torch.version.cuda,
          "torch_device": (
              torch.cuda.get_device_name(torch.cuda.current_device())
              if torch.cuda.is_available()
              else "cpu"
          ),
          "xla_python_client_preallocate": os.environ.get(
              "XLA_PYTHON_CLIENT_PREALLOCATE"
          ),
          "torch_home": os.environ.get("TORCH_HOME"),
      },
  }
  _atomic_json(metrics_path, result)
  print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
