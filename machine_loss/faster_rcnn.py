"""Frozen Faster R-CNN transform/backbone feature loss."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any


@dataclass(frozen=True)
class FeatureMetadata:
  """Shapes and preprocessing facts recorded for one cached feature layer."""

  layer: str
  input_shape_hwc: tuple[int, ...]
  transformed_shape_nchw: tuple[int, ...]
  feature_shape_nchw: tuple[int, ...]


class FrozenFPNFeatureLoss:
  """Computes L1 distance to a cached reference FPN feature map.

  Only ``model.transform`` and ``model.backbone`` are executed. RPN, RoI heads,
  and NMS remain exclusive to the Phase 2 mAP evaluator.
  """

  def __init__(self, model: Any, layer: str = "0"):
    self.model = model.eval()
    self.model.requires_grad_(False)
    self.layer = str(layer)
    self.reference_feature = None
    self.metadata = None
    self._assert_frozen()

  def _assert_frozen(self) -> None:
    if self.model.training:
      raise RuntimeError("Faster R-CNN must be in eval mode")
    if any(parameter.requires_grad for parameter in self.model.parameters()):
      raise RuntimeError("Every detector parameter must be frozen")

  @staticmethod
  def _validate_image(image: Any) -> None:
    if image.ndim != 3 or image.shape[-1] != 3:
      raise ValueError(
          "Expected one HWC RGB image with shape [height, width, 3], got "
          f"{tuple(image.shape)}"
      )
    if not image.is_floating_point():
      raise TypeError(f"Expected floating-point RGB values, got {image.dtype}")

  def _extract(self, image: Any) -> tuple[Any, tuple[int, ...]]:
    self._validate_image(image)
    chw = image.permute(2, 0, 1)
    image_list, _ = self.model.transform([chw], None)
    features = self.model.backbone(image_list.tensors)
    if not isinstance(features, dict):
      features = {"0": features}
    if self.layer not in features:
      raise KeyError(
          f"Missing FPN layer {self.layer!r}; available={tuple(features)}"
      )
    return features[self.layer], tuple(image_list.tensors.shape)

  def cache_reference(self, image: Any) -> FeatureMetadata:
    """Caches the original-image feature without a computation graph."""
    import torch  # pylint: disable=g-import-not-at-top

    self._assert_frozen()
    with torch.no_grad():
      feature, transformed_shape = self._extract(image)
      self.reference_feature = feature.detach().clone()
    self.metadata = FeatureMetadata(
        layer=self.layer,
        input_shape_hwc=tuple(image.shape),
        transformed_shape_nchw=transformed_shape,
        feature_shape_nchw=tuple(self.reference_feature.shape),
    )
    return self.metadata

  def __call__(self, reconstruction: Any) -> Any:
    """Returns feature L1 loss while retaining input-image gradients."""
    import torch  # pylint: disable=g-import-not-at-top

    self._assert_frozen()
    if self.reference_feature is None:
      raise RuntimeError("Call cache_reference before computing feature loss")
    feature, transformed_shape = self._extract(reconstruction)
    if transformed_shape != self.metadata.transformed_shape_nchw:
      raise ValueError(
          "Reference/reconstruction transformed shapes differ: "
          f"{self.metadata.transformed_shape_nchw} != {transformed_shape}"
      )
    if tuple(feature.shape) != self.metadata.feature_shape_nchw:
      raise ValueError(
          "Reference/reconstruction feature shapes differ: "
          f"{self.metadata.feature_shape_nchw} != {tuple(feature.shape)}"
      )
    return torch.mean(torch.abs(feature - self.reference_feature))

  def detector_gradients_are_none(self) -> bool:
    """Returns whether frozen detector parameters accumulated no gradients."""
    return all(parameter.grad is None for parameter in self.model.parameters())


def create_frozen_fpn_loss(device: str, layer: str = "0"):
  """Loads the official COCO_V1 detector and returns a frozen feature loss."""
  from torchvision.models.detection import (  # pylint: disable=g-import-not-at-top
      FasterRCNN_ResNet50_FPN_Weights,
      fasterrcnn_resnet50_fpn,
  )

  weights = FasterRCNN_ResNet50_FPN_Weights.COCO_V1
  model = fasterrcnn_resnet50_fpn(weights=weights).to(device)
  return FrozenFPNFeatureLoss(model=model, layer=layer), weights


def detector_parameter_checksum(model: Any) -> str:
  """Returns a deterministic SHA-256 over detector names, dtypes, and bytes."""
  digest = hashlib.sha256()
  for name, parameter in model.named_parameters():
    value = parameter.detach().cpu().contiguous().numpy()
    digest.update(name.encode("utf-8"))
    digest.update(str(value.dtype).encode("ascii"))
    digest.update(str(value.shape).encode("ascii"))
    digest.update(value.tobytes())
  return digest.hexdigest()
