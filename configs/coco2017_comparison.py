"""Issue-20 NEW fixed baseline (not a reproduction of the seminar results).

Variants: smoke_image_rd1e3, smoke_combined_rd1e3,
pilot_image_rd3e4, pilot_combined_rd3e4 (also rd1e3, rd3e3).
Paths are explicit environment variables; W&B is disabled.
"""
import os
from pathlib import Path
from c3_neural_compression.configs import kodak_machine_pilot

RATES = {'rd3e4': 3e-4, 'rd1e3': 1e-3, 'rd3e3': 3e-3}


def get_config(config_string='smoke_image_rd1e3'):
  try:
    budget, objective, rate = config_string.split('_')
    assert budget in ('smoke', 'pilot') and objective in ('image', 'combined')
    rd_weight = RATES[rate]
  except (ValueError, KeyError, AssertionError) as error:
    raise ValueError('Expected {smoke,pilot}_{image,combined}_{rd3e4,rd1e3,rd3e3}') from error
  required = ('C3_COCO_ROOT', 'C3_COCO_ANNOTATIONS', 'C3_SUBSET_IDS', 'C3_OUTPUT_ROOT')
  missing = [name for name in required if not os.environ.get(name)]
  if missing:
    raise ValueError(f'Set explicit comparison paths: {missing}')
  config = kodak_machine_pilot.get_config()
  config.unlock()
  config.random_seed = 20261004
  exp = config.experiment_kwargs.config
  exp.random_seed = config.random_seed
  exp.dataset.name = 'coco2017'
  exp.dataset.root_dir = os.environ['C3_COCO_ROOT']
  exp.dataset.annotation_file = os.environ['C3_COCO_ANNOTATIONS']
  exp.dataset.subset_ids_file = os.environ['C3_SUBSET_IDS']
  exp.dataset.num_examples = None
  exp.dataset.skip_examples = 0
  exp.loss.rd_weight = rd_weight
  exp.loss.image_weight = 1.0
  exp.loss.machine_weight = 0.0 if objective == 'image' else 0.1
  exp.opt.num_noise_steps = 2 if budget == 'smoke' else 500
  exp.opt.max_num_ste_steps = 0 if budget == 'smoke' else 50
  exp.opt.cosine_decay_schedule_kwargs.decay_steps = exp.opt.num_noise_steps
  exp.quant.kumaraswamy_decay_steps = exp.opt.num_noise_steps
  exp.opt.noise_log_every = 1 if budget == 'smoke' else 10
  exp.output_dir = str(Path(os.environ['C3_OUTPUT_ROOT']) / config_string)
  exp.comparison_guard = {'enabled': True}
  exp.checkpointing.enabled = budget == 'pilot'
  exp.checkpointing.directory = str(Path(exp.output_dir) / 'checkpoints')
  exp.checkpointing.resume = False  # explicit fresh attempt, never hidden best-of-retry
  exp.tracking.enabled = False
  config.lock()
  return config
