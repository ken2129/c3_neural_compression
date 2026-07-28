"""Non-JIT JAX-to-PyTorch gradient bridge for Phase 3 Gate A.

This module deliberately separates two concerns:

* PyTorch computes a scalar loss and its gradient with respect to a JAX-owned
  input buffer shared through DLPack.
* ``jax.custom_vjp`` supplies that image gradient to JAX autodiff.

It does not make arbitrary PyTorch code JIT-compatible. The C3 Gate B path
JITs its JAX base gradient and parameter VJP separately, with this concrete
PyTorch image-gradient computation between those two compiled regions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


TorchLoss = Callable[[Any], Any]


def torch_value_only(image: Any, torch_loss: TorchLoss) -> Any:
  """Returns a scalar loss as a JAX array without building a backward graph."""
  _reject_tracer(image)
  import jax  # pylint: disable=g-import-not-at-top
  import torch  # pylint: disable=g-import-not-at-top

  with torch.no_grad():
    value = torch_loss(torch.utils.dlpack.from_dlpack(image))
  return jax.dlpack.from_dlpack(value.detach())


def _reject_tracer(value: Any) -> None:
  """Fails clearly when Gate A is accidentally placed under ``jax.jit``."""
  import jax  # pylint: disable=g-import-not-at-top

  if isinstance(value, jax.core.Tracer):
    raise RuntimeError(
        "The Phase 3 Gate A PyTorch bridge is non-JIT. Move the call outside "
        "jax.jit or implement the explicit Gate B execution boundary."
    )


def torch_value_and_grad(
    image: Any,
    torch_loss: TorchLoss,
) -> tuple[Any, Any]:
  """Returns a PyTorch scalar loss and image gradient as JAX arrays.

  The input buffer is shared with PyTorch through DLPack. The PyTorch view is
  detached before ``requires_grad`` is enabled, and callers must not mutate it
  in-place. JAX and PyTorch synchronize DLPack producer/consumer streams, but
  their autodiff graphs remain independent.

  Args:
    image: Concrete (non-traced) floating-point JAX array.
    torch_loss: Callable receiving a PyTorch tensor and returning one scalar
      floating-point PyTorch tensor.

  Returns:
    ``(loss, image_gradient)`` as JAX arrays on the input device.
  """
  _reject_tracer(image)

  import jax  # pylint: disable=g-import-not-at-top
  import torch  # pylint: disable=g-import-not-at-top

  torch_view = torch.utils.dlpack.from_dlpack(image)
  if not torch_view.is_floating_point():
    raise TypeError(f"Expected a floating-point image, got {torch_view.dtype}")
  torch_input = torch_view.detach().requires_grad_(True)
  loss = torch_loss(torch_input)
  if not isinstance(loss, torch.Tensor):
    raise TypeError("torch_loss must return a torch.Tensor")
  if loss.ndim != 0:
    raise ValueError(f"torch_loss must return a scalar, got shape {loss.shape}")
  if not loss.is_floating_point():
    raise TypeError(f"torch_loss must return floating point, got {loss.dtype}")
  (image_gradient,) = torch.autograd.grad(
      loss,
      torch_input,
      create_graph=False,
      retain_graph=False,
      allow_unused=False,
  )
  if image_gradient is None:
    raise RuntimeError("PyTorch did not produce an image gradient")

  # Backbone preprocessing commonly creates a non-contiguous HWC gradient
  # after an NHWC/NCHW permutation. JAX preserves DLPack layouts, but a custom
  # VJP must return the same logical layout as its primal input. Materialize a
  # standard contiguous gradient at the framework boundary.
  loss_jax = jax.dlpack.from_dlpack(loss.detach())
  gradient_jax = jax.dlpack.from_dlpack(image_gradient.detach().contiguous())
  return loss_jax, gradient_jax


def make_jax_vjp_loss(torch_loss: TorchLoss) -> Callable[[Any], Any]:
  """Wraps a PyTorch scalar image loss in a non-JIT JAX ``custom_vjp``."""
  import jax  # pylint: disable=g-import-not-at-top

  @jax.custom_vjp
  def jax_loss(image: Any) -> Any:
    loss, _ = torch_value_and_grad(image, torch_loss)
    return loss

  def forward(image: Any) -> tuple[Any, Any]:
    loss, image_gradient = torch_value_and_grad(image, torch_loss)
    return loss, image_gradient

  def backward(image_gradient: Any, cotangent: Any) -> tuple[Any]:
    return (cotangent * image_gradient,)

  jax_loss.defvjp(forward, backward)
  return jax_loss
