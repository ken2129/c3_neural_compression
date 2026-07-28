"""One-image Phase 3 weight-calibration pilot on Kodak."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak


def get_config() -> config_dict.ConfigDict:
  """Returns a resumable 30–40 minute combined-objective pilot."""
  config = kodak.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.dataset.root_dir = "/workspace/datasets/kodak"
  exp.dataset.num_examples = 1
  exp.output_dir = "/workspace/outputs/c3_machine_pilot"

  exp.opt.num_noise_steps = 500
  exp.opt.max_num_ste_steps = 50
  exp.opt.cosine_decay_schedule_kwargs.decay_steps = exp.opt.num_noise_steps
  exp.quant.kumaraswamy_decay_steps = exp.opt.num_noise_steps
  exp.opt.noise_log_every = 10
  exp.opt.ste_log_every = 10

  exp.loss.image_weight = 1.0
  exp.loss.machine_weight = 0.1
  exp.loss.machine.device = "cuda"
  exp.loss.machine.feature_layers = ("0", "1", "2", "3")
  exp.loss.machine.feature_layer_weights = (0.25, 0.25, 0.25, 0.25)

  exp.checkpointing.enabled = True
  exp.checkpointing.directory = (
      "/workspace/outputs/c3_machine_pilot/checkpoints"
  )
  exp.checkpointing.save_every_steps = 50
  exp.checkpointing.resume = True

  exp.tracking.enabled = True
  exp.tracking.project = "c3-neural-compression"
  exp.tracking.run_name = "phase3-kodak-pilot-combined-w0.1"
  exp.tracking.run_id = "phase3-kodak-pilot-combined-w0.1"
  exp.tracking.mode = "online"
  config.lock()
  return config
