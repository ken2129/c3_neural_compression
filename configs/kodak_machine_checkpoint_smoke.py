"""Multi-layer Phase 3 checkpoint/resume smoke test."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak_machine_smoke


def get_config() -> config_dict.ConfigDict:
  """Returns a two-step run that checkpoints every machine-loss step."""
  config = kodak_machine_smoke.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.output_dir = "/workspace/outputs/c3_machine_checkpoint_smoke"
  exp.checkpointing.enabled = True
  exp.checkpointing.directory = (
      "/workspace/outputs/c3_machine_checkpoint_smoke/checkpoints"
  )
  exp.checkpointing.save_every_steps = 1
  exp.checkpointing.resume = True
  config.lock()
  return config
