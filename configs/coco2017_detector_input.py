"""Issue41 500+50 detector-input protocol; budget approval is external."""
import math
from pathlib import Path
from c3_neural_compression.configs import coco2017_comparison


def get_config(config_string='combined_lambda0.01'):
  try:
    objective, value = config_string.split('_lambda')
    rd_weight = float(value)
    if objective not in ('image', 'combined') or not math.isfinite(rd_weight) or rd_weight <= 0:
      raise ValueError('Invalid objective or lambda')
  except (ValueError, TypeError) as error:
    raise ValueError('Expected {image,combined}_lambda<positive finite value>') from error
  config = coco2017_comparison.get_config(f'pilot_{objective}_rd1e3')
  config.unlock()
  experiment = config.experiment_kwargs.config
  experiment.dataset.input_protocol = 'detector_input_v1'
  experiment.loss.rd_weight = rd_weight
  experiment.output_dir = str(Path(experiment.output_dir).parent / ('detector_input_' + config_string))
  experiment.checkpointing.directory = str(Path(experiment.output_dir) / 'checkpoints')
  config.lock()
  return config
