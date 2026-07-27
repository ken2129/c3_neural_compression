# Copyright 2024 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""Short one-image Kodak run for validating the complete C3 pipeline."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak


def get_config() -> config_dict.ConfigDict:
  """Returns a non-benchmark smoke-test config."""
  config = kodak.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.dataset.root_dir = '/workspace/datasets/kodak'
  exp.dataset.skip_examples = 0
  exp.dataset.num_examples = 1
  exp.output_dir = '/workspace/outputs/c3_baseline_smoke'
  exp.opt.num_noise_steps = 100
  exp.opt.max_num_ste_steps = 10
  exp.opt.cosine_decay_schedule_kwargs.decay_steps = exp.opt.num_noise_steps
  exp.quant.kumaraswamy_decay_steps = exp.opt.num_noise_steps
  exp.opt.noise_log_every = 10
  exp.opt.ste_log_every = 5
  config.lock()
  return config
