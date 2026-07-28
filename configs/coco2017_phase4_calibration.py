"""Twenty-image Phase 4 rate calibration configurations."""

import pathlib

from ml_collections import config_dict

from c3_neural_compression.configs import kodak_machine_pilot


_RATE_POINTS = {
    'rd3e4': 3e-4,
    'rd1e3': 1e-3,
    'rd3e3': 3e-3,
}


def get_config(config_string: str = 'image_rd1e3') -> config_dict.ConfigDict:
  """Returns one fixed Gate 2 objective/rate-point configuration."""
  try:
    objective, rate_name = config_string.split('_', maxsplit=1)
    rd_weight = _RATE_POINTS[rate_name]
  except (AttributeError, KeyError, ValueError) as error:
    choices = [
        f'{objective}_{rate}'
        for objective in ('image', 'combined')
        for rate in _RATE_POINTS
    ]
    raise ValueError(
        f'Unknown calibration variant {config_string!r}; choose from {choices}'
    ) from error
  if objective not in ('image', 'combined'):
    raise ValueError(f'Unknown calibration objective: {objective!r}')

  config = kodak_machine_pilot.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  repository_root = pathlib.Path(__file__).resolve().parents[1]
  run_name = f'phase4-calibration-{objective}-{rate_name}'
  output_dir = f'/workspace/outputs/{run_name}'

  exp.dataset.name = 'coco2017'
  exp.dataset.root_dir = '/workspace/datasets/coco2017/val2017'
  exp.dataset.annotation_file = (
      '/workspace/datasets/coco2017/annotations/instances_val2017.json'
  )
  exp.dataset.subset_ids_file = str(
      repository_root / 'manifests' / 'coco2017' / 'calibration_20.json'
  )
  exp.dataset.skip_examples = 0
  exp.dataset.num_examples = 20

  exp.loss.rd_weight = rd_weight
  exp.loss.image_weight = 1.0
  exp.loss.machine_weight = 0.0 if objective == 'image' else 0.1
  exp.output_dir = output_dir
  exp.checkpointing.directory = f'{output_dir}/checkpoints'
  exp.tracking.run_name = run_name
  exp.tracking.run_id = run_name
  exp.tracking.mode = 'online'
  config.lock()
  return config
