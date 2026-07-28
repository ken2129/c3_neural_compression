"""One-image Phase 3 machine-only comparison pilot on Kodak."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak_machine_pilot


def get_config() -> config_dict.ConfigDict:
  """Returns the pilot schedule with rate plus machine distortion."""
  config = kodak_machine_pilot.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.output_dir = "/workspace/outputs/c3_machine_only_pilot"
  exp.loss.image_weight = 0.0
  exp.loss.machine_weight = 0.1
  exp.checkpointing.directory = (
      "/workspace/outputs/c3_machine_only_pilot/checkpoints"
  )
  exp.tracking.run_name = "phase3-kodak-pilot-machine-only-w0.1"
  exp.tracking.run_id = "phase3-kodak-pilot-machine-only-w0.1"
  config.lock()
  return config
