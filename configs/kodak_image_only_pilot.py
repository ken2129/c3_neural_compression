"""One-image Phase 3 image-only comparison pilot on Kodak."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak_machine_pilot


def get_config() -> config_dict.ConfigDict:
  """Returns the pilot schedule with the original image-only objective."""
  config = kodak_machine_pilot.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.output_dir = "/workspace/outputs/c3_image_only_pilot"
  exp.loss.image_weight = 1.0
  exp.loss.machine_weight = 0.0
  exp.checkpointing.directory = (
      "/workspace/outputs/c3_image_only_pilot/checkpoints"
  )
  exp.tracking.run_name = "phase3-kodak-pilot-image-only"
  exp.tracking.run_id = "phase3-kodak-pilot-image-only"
  config.lock()
  return config
