"""Two-image Phase 4 COCO image-only smoke configuration."""

from ml_collections import config_dict

from c3_neural_compression.configs import coco2017_phase4_smoke


def get_config() -> config_dict.ConfigDict:
  """Returns Gate 1 settings with machine loss disabled."""
  config = coco2017_phase4_smoke.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.loss.image_weight = 1.0
  exp.loss.machine_weight = 0.0
  exp.output_dir = '/workspace/outputs/c3_phase4_gate1_image_only'
  config.lock()
  return config
