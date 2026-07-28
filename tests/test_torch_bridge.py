"""CPU tests for the non-JIT Phase 3 JAX/PyTorch bridge."""

import unittest

import jax
import jax.numpy as jnp
import numpy as np
import torch

from c3_neural_compression.machine_loss import faster_rcnn
from c3_neural_compression.machine_loss import torch_bridge


class _ImageList:

  def __init__(self, tensors):
    self.tensors = tensors


class _IdentityTransform(torch.nn.Module):

  def forward(self, images, targets=None):
    return _ImageList(torch.stack(images)), targets


class _ToyBackbone(torch.nn.Module):

  def __init__(self):
    super().__init__()
    self.scale = torch.nn.Parameter(torch.tensor(2.0))

  def forward(self, tensors):
    return {
        "0": tensors * self.scale,
        "1": tensors * self.scale * 3.0,
    }


class _ToyDetector(torch.nn.Module):

  def __init__(self):
    super().__init__()
    self.transform = _IdentityTransform()
    self.backbone = _ToyBackbone()


class TorchBridgeTest(unittest.TestCase):

  def test_quadratic_value_and_gradient(self):
    loss = torch_bridge.make_jax_vjp_loss(
        lambda tensor: torch.mean(tensor.square())
    )
    image = jnp.arange(6, dtype=jnp.float32).reshape(2, 3)
    value, gradient = jax.value_and_grad(loss)(image)
    np.testing.assert_allclose(value, np.mean(np.arange(6) ** 2))
    np.testing.assert_allclose(
        gradient, 2 * np.arange(6, dtype=np.float32).reshape(2, 3) / 6
    )

  def test_bridge_gradient_matches_directional_finite_difference(self):
    loss = torch_bridge.make_jax_vjp_loss(
        lambda tensor: torch.mean((tensor - 0.25).square())
    )
    image = jnp.linspace(0.0, 1.0, 12, dtype=jnp.float32).reshape(2, 2, 3)
    direction = jnp.cos(jnp.arange(12, dtype=jnp.float32)).reshape(image.shape)
    gradient = jax.grad(loss)(image)
    epsilon = 1e-3
    finite_difference = (
        loss(image + epsilon * direction)
        - loss(image - epsilon * direction)
    ) / (2 * epsilon)
    predicted = jnp.vdot(gradient, direction)
    np.testing.assert_allclose(finite_difference, predicted, rtol=2e-3)

  def test_jit_fails_with_gate_a_message(self):
    loss = torch_bridge.make_jax_vjp_loss(lambda tensor: tensor.square().mean())
    with self.assertRaisesRegex(RuntimeError, "Gate A.*non-JIT"):
      jax.jit(loss)(jnp.ones((2,), dtype=jnp.float32))

  def test_bridge_chains_to_jax_toy_parameters(self):
    loss = torch_bridge.make_jax_vjp_loss(
        lambda tensor: torch.mean((tensor - 0.5).square())
    )

    def toy_synthesis(parameter):
      reconstruction = jnp.reshape(parameter, (2, 2, 3)) * 0.25
      return loss(reconstruction)

    parameter = jnp.linspace(0.1, 0.9, 12, dtype=jnp.float32)
    gradient = jax.grad(toy_synthesis)(parameter)
    self.assertTrue(bool(jnp.all(jnp.isfinite(gradient))))
    self.assertGreater(float(jnp.linalg.norm(gradient)), 0.0)

  def test_feature_loss_cache_freeze_and_input_gradient(self):
    feature_loss = faster_rcnn.FrozenFPNFeatureLoss(_ToyDetector(), layer="0")
    reference = torch.zeros((4, 5, 3), dtype=torch.float32)
    metadata = feature_loss.cache_reference(reference)
    self.assertEqual(metadata.input_shape_hwc, (4, 5, 3))
    self.assertEqual(metadata.transformed_shape_nchw, (1, 3, 4, 5))
    self.assertEqual(metadata.feature_shape_nchw, (1, 3, 4, 5))
    self.assertFalse(feature_loss.reference_feature.requires_grad)

    same = feature_loss(reference)
    self.assertEqual(same.item(), 0.0)

    reconstruction = torch.full(
        (4, 5, 3), 0.1, dtype=torch.float32, requires_grad=True
    )
    changed = feature_loss(reconstruction)
    changed.backward()
    self.assertGreater(changed.item(), 0.0)
    self.assertTrue(torch.isfinite(reconstruction.grad).all())
    self.assertGreater(torch.linalg.vector_norm(reconstruction.grad).item(), 0.0)
    self.assertTrue(feature_loss.detector_gradients_are_none())
    checksum = faster_rcnn.detector_parameter_checksum(feature_loss.model)
    self.assertEqual(len(checksum), 64)
    self.assertEqual(
        checksum, faster_rcnn.detector_parameter_checksum(feature_loss.model)
    )

  def test_fpn_loss_through_jax_bridge(self):
    feature_loss = faster_rcnn.FrozenFPNFeatureLoss(_ToyDetector(), layer="0")
    feature_loss.cache_reference(torch.zeros((3, 4, 3), dtype=torch.float32))
    loss = torch_bridge.make_jax_vjp_loss(feature_loss)
    reconstruction = jnp.full((3, 4, 3), 0.2, dtype=jnp.float32)
    value, gradient = jax.value_and_grad(loss)(reconstruction)
    self.assertGreater(float(value), 0.0)
    self.assertTrue(bool(jnp.all(jnp.isfinite(gradient))))
    self.assertGreater(float(jnp.linalg.norm(gradient)), 0.0)
    self.assertTrue(feature_loss.detector_gradients_are_none())

  def test_weighted_multi_layer_fpn_loss_and_gradient(self):
    feature_loss = faster_rcnn.FrozenFPNFeatureLoss(
        _ToyDetector(),
        layers=("0", "1"),
        layer_weights=(2.0, 0.5),
    )
    metadata = feature_loss.cache_reference(
        torch.zeros((2, 3, 3), dtype=torch.float32)
    )
    self.assertEqual(tuple(value.layer for value in metadata), ("0", "1"))
    reconstruction = torch.full(
        (2, 3, 3), 0.1, dtype=torch.float32, requires_grad=True
    )
    loss = feature_loss(reconstruction)
    self.assertAlmostEqual(loss.item(), 0.7, places=6)
    loss.backward()
    self.assertTrue(torch.isfinite(reconstruction.grad).all())
    self.assertGreater(torch.linalg.vector_norm(reconstruction.grad).item(), 0)

  def test_multi_layer_config_validation(self):
    with self.assertRaisesRegex(ValueError, "equal length"):
      faster_rcnn.FrozenFPNFeatureLoss(
          _ToyDetector(), layers=("0", "1"), layer_weights=(1.0,)
      )
    with self.assertRaisesRegex(ValueError, "must be positive"):
      faster_rcnn.FrozenFPNFeatureLoss(
          _ToyDetector(), layers=("0", "1"), layer_weights=(0.0, 0.0)
      )


if __name__ == "__main__":
  unittest.main()
