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

  def __init__(
      self,
      model: Any,
      layer: str = "0",
      layers: tuple[str, ...] | None = None,
      layer_weights: tuple[float, ...] | None = None,
  ):
    self.model = model.eval()
    self.model.requires_grad_(False)
    self.layers = tuple(str(value) for value in (layers or (layer,)))
    if not self.layers:
      raise ValueError("At least one FPN feature layer is required")
    if layer_weights is None:
      layer_weights = (1.0,) * len(self.layers)
    self.layer_weights = tuple(float(value) for value in layer_weights)
    if len(self.layer_weights) != len(self.layers):
      raise ValueError("FPN feature layers and weights must have equal length")
    if any(value < 0 for value in self.layer_weights):
      raise ValueError("FPN feature weights must be non-negative")
    if not any(value > 0 for value in self.layer_weights):
      raise ValueError("At least one FPN feature weight must be positive")
    self.layer = self.layers[0]
    self.reference_feature = None
    self.reference_features = None
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

  def _extract(self, image: Any) -> tuple[dict[str, Any], tuple[int, ...]]:
    self._validate_image(image)
    chw = image.permute(2, 0, 1)
    image_list, _ = self.model.transform([chw], None)
    features = self.model.backbone(image_list.tensors)
    if not isinstance(features, dict):
      features = {"0": features}
    missing = tuple(layer for layer in self.layers if layer not in features)
    if missing:
      raise KeyError(
          f"Missing FPN layers {missing!r}; available={tuple(features)}"
      )
    return {layer: features[layer] for layer in self.layers}, tuple(
        image_list.tensors.shape
    )

  def cache_reference(self, image: Any) -> FeatureMetadata:
    """Caches the original-image feature without a computation graph."""
    import torch  # pylint: disable=g-import-not-at-top

    self._assert_frozen()
    with torch.no_grad():
      features, transformed_shape = self._extract(image)
      self.reference_features = {
          layer: feature.detach().clone()
          for layer, feature in features.items()
      }
    self.reference_feature = self.reference_features[self.layer]
    metadata = tuple(
        FeatureMetadata(
            layer=layer,
            input_shape_hwc=tuple(image.shape),
            transformed_shape_nchw=transformed_shape,
            feature_shape_nchw=tuple(self.reference_features[layer].shape),
        )
        for layer in self.layers
    )
    self.metadata = metadata[0] if len(metadata) == 1 else metadata
    return self.metadata

  def __call__(self, reconstruction: Any) -> Any:
    """Returns feature L1 loss while retaining input-image gradients."""
    total, _ = self.loss_components(reconstruction)
    return total

  def loss_components(self, reconstruction: Any) -> tuple[Any, dict[str, Any]]:
    """Returns weighted total and unweighted per-layer feature losses."""
    import torch  # pylint: disable=g-import-not-at-top

    self._assert_frozen()
    if self.reference_feature is None:
      raise RuntimeError("Call cache_reference before computing feature loss")
    features, transformed_shape = self._extract(reconstruction)
    metadata = (self.metadata,) if len(self.layers) == 1 else self.metadata
    losses = {}
    for layer, weight, layer_metadata in zip(
        self.layers, self.layer_weights, metadata
    ):
      feature = features[layer]
      if transformed_shape != layer_metadata.transformed_shape_nchw:
        raise ValueError(
            "Reference/reconstruction transformed shapes differ: "
            f"{layer_metadata.transformed_shape_nchw} != {transformed_shape}"
        )
      if tuple(feature.shape) != layer_metadata.feature_shape_nchw:
        raise ValueError(
            f"Reference/reconstruction feature shapes differ for {layer}: "
            f"{layer_metadata.feature_shape_nchw} != {tuple(feature.shape)}"
        )
      losses[layer] = torch.mean(
          torch.abs(feature - self.reference_features[layer])
      )
    return sum(
        weight * losses[layer]
        for layer, weight in zip(self.layers, self.layer_weights)
    ), losses

  def detector_gradients_are_none(self) -> bool:
    """Returns whether frozen detector parameters accumulated no gradients."""
    return all(parameter.grad is None for parameter in self.model.parameters())


def create_frozen_fpn_loss(
    device: str,
    layer: str = "0",
    layers: tuple[str, ...] | None = None,
    layer_weights: tuple[float, ...] | None = None,
):
  """Loads the official COCO_V1 detector and returns a frozen feature loss."""
  from torchvision.models.detection import (  # pylint: disable=g-import-not-at-top
      FasterRCNN_ResNet50_FPN_Weights,
      fasterrcnn_resnet50_fpn,
  )

  weights = FasterRCNN_ResNet50_FPN_Weights.COCO_V1
  model = fasterrcnn_resnet50_fpn(weights=weights).to(device)
  return FrozenFPNFeatureLoss(
      model=model,
      layer=layer,
      layers=layers,
      layer_weights=layer_weights,
  ), weights


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
