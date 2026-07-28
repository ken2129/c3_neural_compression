"""Differentiable machine-oriented losses for C3."""

from c3_neural_compression.machine_loss.torch_bridge import make_jax_vjp_loss
from c3_neural_compression.machine_loss.torch_bridge import torch_value_and_grad

__all__ = ["make_jax_vjp_loss", "torch_value_and_grad"]
