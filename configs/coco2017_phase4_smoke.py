"""Two-image Phase 4 COCO compression smoke configuration."""

import pathlib

from ml_collections import config_dict

from c3_neural_compression.configs import kodak_machine_smoke


def get_config() -> config_dict.ConfigDict:
  """Returns the short combined-objective Gate 1 configuration."""
  config = kodak_machine_smoke.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  repository_root = pathlib.Path(__file__).resolve().parents[1]
  exp.dataset.name = 'coco2017'
  exp.dataset.root_dir = '/workspace/datasets/coco2017/val2017'
  exp.dataset.annotation_file = (
      '/workspace/datasets/coco2017/annotations/instances_val2017.json'
  )
  exp.dataset.subset_ids_file = str(
      repository_root / 'manifests' / 'coco2017' / 'smoke.json'
  )
  exp.dataset.skip_examples = 0
  exp.dataset.num_examples = 2
  exp.output_dir = '/workspace/outputs/c3_phase4_gate1_combined'
  exp.checkpointing.enabled = False
  exp.tracking.enabled = False
  config.lock()
  return config
