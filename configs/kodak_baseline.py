# Copyright 2024 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""Official-step one-image Kodak baseline with persistent outputs."""

from ml_collections import config_dict

from c3_neural_compression.configs import kodak


def get_config() -> config_dict.ConfigDict:
  """Returns the official Kodak settings restricted to the first image."""
  config = kodak.get_config()
  config.unlock()
  exp = config.experiment_kwargs.config
  exp.dataset.root_dir = '/workspace/datasets/kodak'
  exp.dataset.skip_examples = 0
  exp.dataset.num_examples = 1
  exp.output_dir = '/workspace/outputs/c3_baseline_official'
  exp.checkpointing.enabled = True
  exp.checkpointing.directory = (
      '/workspace/outputs/c3_baseline_official/checkpoints'
  )
  exp.checkpointing.save_every_steps = 5_000
  exp.checkpointing.resume = True
  exp.tracking.enabled = True
  exp.tracking.project = 'c3-neural-compression'
  exp.tracking.run_name = 'kodak01-official-rd-0.001'
  exp.tracking.run_id = 'kodak01-official-rd-0001'
  exp.tracking.mode = 'online'
  config.lock()
  return config
