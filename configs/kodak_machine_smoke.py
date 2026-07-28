"""One-image Phase 3 integration smoke test on Kodak."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak_smoke


def get_config() -> config_dict.ConfigDict:
  """Returns a short combined image/machine-loss config."""
  config = kodak_smoke.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.output_dir = "/workspace/outputs/c3_machine_smoke"
  exp.opt.num_noise_steps = 2
  exp.opt.max_num_ste_steps = 0
  exp.opt.cosine_decay_schedule_kwargs.decay_steps = exp.opt.num_noise_steps
  exp.quant.kumaraswamy_decay_steps = exp.opt.num_noise_steps
  exp.opt.noise_log_every = 1
  # Keep both logged steps comparable: no changing stochastic quantization.
  exp.quant.noise_quant_type = "ste"
  exp.quant.use_kumaraswamy_noise = False
  exp.loss.image_weight = 1.0
  exp.loss.machine_weight = 1.0
  exp.loss.machine.device = "cuda"
  exp.loss.machine.feature_layer = "0"
  exp.checkpointing.enabled = False
  exp.tracking.enabled = False
  config.lock()
  return config
