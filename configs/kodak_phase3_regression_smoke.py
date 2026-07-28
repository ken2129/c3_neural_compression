"""Phase 1 image-only smoke rerun through the Phase 3 codebase."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak_smoke


def get_config() -> config_dict.ConfigDict:
  """Returns an isolated lambda_machine=0 regression run."""
  config = kodak_smoke.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.output_dir = "/workspace/outputs/c3_phase3_regression_smoke"
  exp.loss.image_weight = 1.0
  exp.loss.machine_weight = 0.0
  exp.checkpointing.enabled = False
  exp.tracking.enabled = False
  config.lock()
  return config
