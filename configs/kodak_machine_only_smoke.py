"""One-image machine-only Phase 3 integration smoke test on Kodak."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak_machine_smoke


def get_config() -> config_dict.ConfigDict:
  """Returns a short machine-only config while retaining the rate term."""
  config = kodak_machine_smoke.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.output_dir = "/workspace/outputs/c3_machine_only_smoke"
  exp.loss.image_weight = 0.0
  exp.loss.machine_weight = 1.0
  config.lock()
  return config
