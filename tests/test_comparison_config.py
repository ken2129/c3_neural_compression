"""New baseline configuration tests, no JAX training required."""
import os
import unittest
from unittest import mock
from c3_neural_compression.configs import coco2017_comparison as config


class ComparisonConfigTest(unittest.TestCase):
  def test_paths_required(self):
    with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'explicit comparison paths'):
      config.get_config()

  def test_objectives_share_budget_seed_and_features(self):
    env = {name:'/tmp/test' for name in ('C3_COCO_ROOT','C3_COCO_ANNOTATIONS','C3_SUBSET_IDS','C3_OUTPUT_ROOT')}
    with mock.patch.dict(os.environ, env):
      for budget,steps in [('smoke',2),('pilot',500)]:
        image = config.get_config(f'{budget}_image_rd1e3').experiment_kwargs.config
        combined = config.get_config(f'{budget}_combined_rd1e3').experiment_kwargs.config
        self.assertEqual(image.random_seed, combined.random_seed)
        self.assertEqual(image.loss.machine_weight, 0)
        self.assertEqual(combined.loss.machine_weight, .1)
        self.assertEqual(image.opt, combined.opt)
        self.assertEqual(image.opt.num_noise_steps, steps)
        self.assertEqual(combined.loss.machine.feature_layers, ('0','1','2','3'))
        self.assertEqual(combined.loss.machine.feature_layer_weights, (.25,)*4)
        self.assertFalse(combined.tracking.enabled)
        self.assertFalse(combined.checkpointing.resume)
        self.assertIn('image_id', image.seed_rule)

  def test_bad_variants(self):
    for variant in ('invalid','pilot_other_rd1e3','smoke_image_unknown'):
      with self.subTest(variant=variant), self.assertRaises(ValueError):
        config.get_config(variant)
