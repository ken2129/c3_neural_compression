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
  config.lock()
  return config
