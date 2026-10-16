"""Val-only adapter for the published TransTIC object-detection checkpoints.

The referenced implementation is NYCU-MAPL/TransTIC at ``UPSTREAM_COMMIT``:
``examples/detection.py::test_epoch``, ``examples/utils/alignment.py``,
``config/detection.yaml``, and ``compressai/zoo/image.py``. This adapter does not
import the training entry point or construct training/Kodak datasets, optimizers,
or a task-loss backbone. It uses the upstream model, not a reimplementation.

The result is floating-point RGB at the Detectron2 mapper's image size. Detector
channel conversion, multiplication by 255, mean subtraction, and padding belong
to the shared detector and must happen exactly once outside this module. Rates
are likelihood estimates, not measured entropy-coded bitstream sizes.

All third-party imports are lazy. Importing this module, calculating dimensions,
and inspecting checkpoint/source metadata require only the Python standard
library. Upstream's root license is Apache-2.0; its bundled CompressAI code also
retains BSD-3-Clause-Clear notices. Preserve those upstream notices separately.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import importlib
import math
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any


UPSTREAM_URL = "https://github.com/NYCU-MAPL/TransTIC"
UPSTREAM_COMMIT = "1132f62cbddc0122aa19f620453c3b0c4de49b2c"
MODEL_NAME = "tic_promptmodel_first2"
DETECTOR_SHORT_EDGE = 800
DETECTOR_MAX_SIZE = 1333
CODEC_DIVISOR = 256

# Architecture-affecting values from the pinned config/detection.yaml. Lambda
# and optimizer settings do not affect eval-mode forward reconstruction.
_PROMPT_CONFIG = {
    "LOCATION": "prepend", "DEEP": True, "NUM_TOKENS": 16,
    "INITIATION": "random", "PROJECT": -1, "DROPOUT": 0.0,
    "TRANSFER_TYPE": "prompt", "ARCHITECT": "both", "WINDOW": "same",
    "HYPERPRIOR": False, "RETURN_ATTENTION": False, "MODEL_DECODER": False,
    "MASK_DOWNSAMPLE": 2, "DECODER_BLOCK": (1, 2, 3, 4),
}


class TransTICError(RuntimeError):
  """A pinned source, checkpoint, dependency, or codec contract is invalid."""


def _quality(quality: int) -> int:
  if isinstance(quality, bool) or not isinstance(quality, int) or quality not in range(1, 5):
    raise ValueError("Published TransTIC detection quality must be an integer 1..4")
  return quality


def checkpoint_url(quality: int) -> str:
  """Official release URL, as linked by the pinned upstream README."""
  return f"{UPSTREAM_URL}/releases/download/v1.0/det_{_quality(quality)}.pth.tar"


def prompt_config() -> SimpleNamespace:
  """Returns fresh, exact detection prompt options for upstream's constructor."""
  values = dict(_PROMPT_CONFIG)
  values["DECODER_BLOCK"] = list(values["DECODER_BLOCK"])
  return SimpleNamespace(**values)


def _positive_dimension(value: int, name: str) -> int:
  if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
    raise ValueError(f"{name} must be a positive integer")
  return value


@dataclass(frozen=True)
class ImageDimensions:
  """Every size is (height, width), with the codec size rounded UP, not padded."""

  original_hw: tuple[int, int]
  detector_hw: tuple[int, int]
  codec_hw: tuple[int, int]

  def as_dict(self) -> dict[str, list[int]]:
    return {name: list(getattr(self, name))
            for name in ("original_hw", "detector_hw", "codec_hw")}


def compute_dimensions(height: int, width: int) -> ImageDimensions:
  """Matches D2 ResizeShortestEdge's scale/cap and int(value + 0.5) rounding."""
  height = _positive_dimension(height, "height")
  width = _positive_dimension(width, "width")
  scale = DETECTOR_SHORT_EDGE / min(height, width)
  if height < width:
    new_height, new_width = DETECTOR_SHORT_EDGE, width * scale
  else:
    new_height, new_width = height * scale, DETECTOR_SHORT_EDGE
  if max(new_height, new_width) > DETECTOR_MAX_SIZE:
    scale = DETECTOR_MAX_SIZE / max(new_height, new_width)
    new_height, new_width = new_height * scale, new_width * scale
  detector_hw = (int(new_height + 0.5), int(new_width + 0.5))
  if min(detector_hw) <= 0:
    raise ValueError("Image aspect ratio produces an empty detector dimension")
  codec_hw = tuple(((size + CODEC_DIVISOR - 1) // CODEC_DIVISOR) * CODEC_DIVISOR
                   for size in detector_hw)
  return ImageDimensions((height, width), detector_hw, codec_hw)


@dataclass(frozen=True)
class Reconstruction:
  """One reconstruction, retained in float32 on the codec's device."""

  rgb: Any  # torch.Tensor, CHW in [0,1], at detector_hw; no PNG round-trip.
  bits_y: float
  bits_z: float
  original_hw: tuple[int, int]
  detector_hw: tuple[int, int]
  codec_hw: tuple[int, int]

  @property
  def estimated_bits(self) -> dict[str, float]:
    return {"y": self.bits_y, "z": self.bits_z,
            "total": self.bits_y + self.bits_z}

  def as_dict(self) -> dict[str, Any]:
    """JSON-safe rate/dimension record; never serializes the image tensor."""
    return {
        "estimated_bits": self.estimated_bits,
        "original_hw": list(self.original_hw),
        "detector_hw": list(self.detector_hw),
        "codec_hw": list(self.codec_hw),
        "rate_kind": "estimated_likelihood_bits",
        "reconstruction_precision": "float32_no_png_quantization",
    }


def _require_torch():
  try:
    return importlib.import_module("torch")
  except ImportError as error:
    raise TransTICError("TransTIC inference requires PyTorch in an isolated environment") from error


def _sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(block)
  return digest.hexdigest()


def verify_upstream(repo_path: Path | str) -> Path:
  """Requires the exact official source commit and unmodified inference code."""
  repo = Path(repo_path).expanduser().resolve()
  for relative in ("compressai/zoo/image.py", "compressai/models/tic_promptmodel.py",
                   "config/detection.yaml", "examples/detection.py",
                   "examples/utils/alignment.py", "LICENSE"):
    if not (repo / relative).is_file():
      raise TransTICError(f"Missing TransTIC upstream file: {repo / relative}")
  try:
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
        stderr=subprocess.PIPE).strip()
    # Editable installation generates version.py. Ignore that generated version
    # file while rejecting modifications to any tracked inference source.
    changed = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--name-only", "HEAD", "--",
         "compressai", "config/detection.yaml", "examples/detection.py",
         "examples/utils/alignment.py"], text=True, stderr=subprocess.PIPE)
  except (OSError, subprocess.CalledProcessError) as error:
    raise TransTICError(f"Cannot verify the TransTIC source checkout at {repo}") from error
  if head != UPSTREAM_COMMIT:
    raise TransTICError(f"TransTIC HEAD is {head}; required {UPSTREAM_COMMIT}")
  changes = [name for name in changed.splitlines()
             if name != "compressai/version.py"]
  if changes:
    raise TransTICError(f"Pinned TransTIC inference sources were modified: {changes}")
  return repo


def _load_upstream_zoo(repo: Path):
  """Never silently substitute a PyPI CompressAI installation for TransTIC."""
  existing = sys.modules.get("compressai")
  expected = repo / "compressai" / "__init__.py"
  if existing is not None and Path(getattr(existing, "__file__", "")).resolve() != expected:
    raise TransTICError(
        "Another compressai is already imported; use a fresh isolated process "
        "with the pinned TransTIC editable install")
  sys.path.insert(0, str(repo))
  try:
    package = importlib.import_module("compressai")
    if Path(package.__file__).resolve() != expected:
      raise TransTICError("Imported compressai is not the pinned TransTIC source")
    zoo = importlib.import_module("compressai.zoo")
  except (ImportError, OSError) as error:
    raise TransTICError(
        "Cannot import pinned TransTIC; install that checkout (including its "
        "C++ extensions), a compatible PyTorch/torchvision, timm and click "
        "in an isolated environment") from error
  finally:
    sys.path.remove(str(repo))
  return zoo


def _checkpoint_state_dict(checkpoint: Any) -> dict[str, Any]:
  if not isinstance(checkpoint, Mapping):
    raise TransTICError("TransTIC checkpoint must contain a state_dict mapping")
  state = checkpoint.get("state_dict")
  if not isinstance(state, Mapping) or not state:
    raise TransTICError("TransTIC checkpoint must contain a nonempty state_dict")
  if any(not isinstance(key, str) for key in state):
    raise TransTICError("Checkpoint state_dict keys must be strings")
  prefixed = [key.startswith("module.") for key in state]
  if any(prefixed) and not all(prefixed):
    raise TransTICError("Checkpoint mixes DataParallel and unprefixed keys")
  strip_prefix = all(prefixed)
  return {(key[7:] if strip_prefix else key): value
          for key, value in state.items()}


def load_published_model(repo_path: Path | str, checkpoint_path: Path | str,
                         quality: int, device: str = "cuda"):
  """Loads one trusted local det_1..4 release checkpoint with strict keys.

  Does not download a checkpoint, run training setup, or invoke entropy coding.
  weights_only=True is deliberate: unsafe pickle fallback is never automatic.
  The release README supplies URLs but no digests; callers must acquire the
  matching quality's file from the official release and retain its SHA256.
  """
  _quality(quality)
  repo = verify_upstream(repo_path)
  checkpoint_file = Path(checkpoint_path).expanduser().resolve()
  if not checkpoint_file.is_file():
    raise TransTICError(f"Missing local TransTIC checkpoint: {checkpoint_file}")
  torch = _require_torch()
  zoo = _load_upstream_zoo(repo)
  model = zoo.image_models[MODEL_NAME](quality=quality, prompt_config=prompt_config())
  try:
    checkpoint = torch.load(str(checkpoint_file), map_location="cpu", weights_only=True)
  except Exception as error:
    raise TransTICError(
        f"Cannot safely load {checkpoint_file}; a compatible PyTorch and the "
        "official weights-only-compatible checkpoint are required. Unsafe "
        "pickle loading was not attempted") from error
  model.load_state_dict(_checkpoint_state_dict(checkpoint), strict=True)
  model.eval()
  model.requires_grad_(False)
  model = model.to(device)
  if model.training or any(parameter.requires_grad for parameter in model.parameters()):
    raise TransTICError("TransTIC model must be frozen and in evaluation mode")
  return model


def mapper_bgr(image_bgr):
  """Runs the actual pinned-protocol D2 uint8 test transform, without tensors."""
  import numpy as np
  if (not isinstance(image_bgr, np.ndarray) or image_bgr.dtype != np.uint8
      or image_bgr.ndim != 3 or image_bgr.shape[2] != 3):
    raise ValueError("Expected original HWC uint8 BGR image")
  dimensions = compute_dimensions(*image_bgr.shape[:2])
  try:
    transforms = importlib.import_module("detectron2.data.transforms")
  except ImportError as error:
    raise TransTICError("The faithful uint8 image mapper requires Detectron2") from error
  transform = transforms.ResizeShortestEdge(
      [DETECTOR_SHORT_EDGE, DETECTOR_SHORT_EDGE], DETECTOR_MAX_SIZE)
  mapped = transform.get_transform(image_bgr).apply_image(image_bgr)
  if mapped.dtype != np.uint8 or tuple(mapped.shape[:2]) != dimensions.detector_hw:
    raise TransTICError("Detectron2 mapper violated the uint8/800/1333 contract")
  return mapped, dimensions


def reconstruct_detector_rgb(rgb, model, original_hw: tuple[int, int]) -> Reconstruction:
  """Reconstructs one already-mapped CHW RGB float tensor without a second map.

  The caller must have used the D2 uint8 mapper, then channel-flipped and divided
  by 255. This helper is also a dependency-light CPU test seam for the codec
  boundary. No per-image fitting, padding, or PNG encoding happens here.
  """
  torch = _require_torch()
  dimensions = compute_dimensions(*original_hw)
  if (not isinstance(rgb, torch.Tensor) or rgb.dtype != torch.float32
      or tuple(rgb.shape) != (3, *dimensions.detector_hw)):
    raise ValueError("Expected float32 CHW RGB tensor at the mapped detector size")
  if not torch.isfinite(rgb).all().item() or rgb.min().item() < 0 or rgb.max().item() > 1:
    raise ValueError("Input RGB values must be finite and in [0,1]")
  if getattr(model, "training", True):
    raise TransTICError("TransTIC model must be in evaluation mode")
  with torch.inference_mode():
    image = rgb.unsqueeze(0)
    if dimensions.codec_hw != dimensions.detector_hw:
      image = torch.nn.functional.interpolate(
          image, size=dimensions.codec_hw, mode="bilinear", align_corners=False)
    output = model(image)
    if not isinstance(output, Mapping) or not isinstance(output.get("likelihoods"), Mapping):
      raise TransTICError("Model output must contain x_hat and y/z likelihoods")
    likelihoods = output["likelihoods"]
    if set(likelihoods) != {"y", "z"}:
      raise TransTICError("Expected exactly y and z likelihood tensors")
    bits = {}
    for name in ("y", "z"):
      values = likelihoods[name]
      if (not isinstance(values, torch.Tensor) or values.numel() == 0
          or not torch.isfinite(values).all().item()
          or values.min().item() <= 0 or values.max().item() > 1):
        raise TransTICError(f"Invalid {name} likelihoods; expected finite probabilities in (0,1]")
      bits[name] = float((torch.log(values).sum() / -math.log(2)).item())
      if not math.isfinite(bits[name]):
        raise TransTICError(f"Nonfinite estimated {name} bit count")
    x_hat = output.get("x_hat")
    if (not isinstance(x_hat, torch.Tensor)
        or tuple(x_hat.shape) != (1, 3, *dimensions.codec_hw)
        or not torch.isfinite(x_hat).all().item()):
      raise TransTICError("Model x_hat must be a finite NCHW tensor at codec size")
    if dimensions.codec_hw != dimensions.detector_hw:
      x_hat = torch.nn.functional.interpolate(
          x_hat, size=dimensions.detector_hw, mode="bilinear", align_corners=False)
    x_hat = x_hat.clamp_(0, 1)[0].to(dtype=torch.float32).detach()
  return Reconstruction(x_hat, bits["y"], bits["z"], dimensions.original_hw,
                        dimensions.detector_hw, dimensions.codec_hw)


class TransTICAdapter:
  """Lazy, val-only local checkpoint loader and per-image reconstruction API."""

  def __init__(self, repo_path: Path | str, checkpoint_path: Path | str,
               quality: int, device: str = "cuda"):
    self.repo_path = Path(repo_path).expanduser().resolve()
    self.checkpoint_path = Path(checkpoint_path).expanduser().resolve()
    self.quality = _quality(quality)
    self.device = device
    self._model = None

  def load(self):
    """Loads and freezes one model once; the source and keys are checked strictly."""
    if self._model is None:
      self._model = load_published_model(
          self.repo_path, self.checkpoint_path, self.quality, self.device)
    return self

  def metadata(self) -> dict[str, Any]:
    """Records local source/checkpoint evidence without importing GPU libraries."""
    verify_upstream(self.repo_path)
    if not self.checkpoint_path.is_file():
      raise TransTICError(f"Missing local TransTIC checkpoint: {self.checkpoint_path}")
    return {
        "upstream_url": UPSTREAM_URL, "upstream_commit": UPSTREAM_COMMIT,
        "upstream_path": str(self.repo_path), "model": MODEL_NAME,
        "quality": self.quality, "model_channels": {"N": 128, "M": 192},
        "prompt_config": vars(prompt_config()),
        "checkpoint_path": str(self.checkpoint_path),
        "checkpoint_sha256": _sha256(self.checkpoint_path),
        "expected_checkpoint_url": checkpoint_url(self.quality),
        "checkpoint_authenticity": "local_sha256_recorded_no_published_digest",
        "device": self.device, "rate_kind": "estimated_likelihood_bits",
        "mapper": {"format": "BGR", "short_edge": DETECTOR_SHORT_EDGE,
                   "max_size": DETECTOR_MAX_SIZE, "interpolation": "Detectron2 uint8 PIL bilinear"},
        "codec_alignment": {"divisor": CODEC_DIVISOR, "mode": "resize_up",
                            "interpolation": "bilinear", "align_corners": False},
        "reconstruction_precision": "float32_no_png_quantization",
        "license_notes": "Apache-2.0 root; bundled CompressAI BSD-3-Clause-Clear notices retained upstream",
    }

  def reconstruct(self, path_or_bgr_array) -> Reconstruction:
    """Accepts a local image path or an original HWC uint8 BGR numpy array."""
    if isinstance(path_or_bgr_array, (str, Path)):
      return self.reconstruct_image(path_or_bgr_array)
    return self.reconstruct_bgr(path_or_bgr_array)

  def reconstruct_image(self, path: Path | str) -> Reconstruction:
    """Reads using the same D2 BGR/EXIF behavior as the original val mapper."""
    try:
      detection_utils = importlib.import_module("detectron2.data.detection_utils")
    except ImportError as error:
      raise TransTICError("Image-path reconstruction requires Detectron2's reader") from error
    image_bgr = detection_utils.read_image(str(path), format="BGR")
    return self.reconstruct_bgr(image_bgr)

  def reconstruct_bgr(self, image_bgr) -> Reconstruction:
    """Maps original BGR once, converts RGB float, and reconstructs one image."""
    import numpy as np
    mapped, dimensions = mapper_bgr(image_bgr)
    torch = _require_torch()
    # Match upstream's uint8 BGR -> float /255 -> channel flip order exactly.
    rgb = torch.as_tensor(np.ascontiguousarray(mapped.transpose(2, 0, 1)))
    rgb = rgb.float().div(255).flip(0).to(self.device)
    self.load()
    return reconstruct_detector_rgb(rgb, self._model, dimensions.original_hw)
