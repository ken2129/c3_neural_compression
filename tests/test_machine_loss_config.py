"""Tests for Phase 3 image/machine objective modes."""

import unittest

from c3_neural_compression.configs import kodak
from c3_neural_compression.configs import kodak_machine_only_smoke
from c3_neural_compression.configs import kodak_machine_smoke


class MachineLossConfigTest(unittest.TestCase):

  def test_image_only_is_the_default(self):
    loss = kodak.get_config().experiment_kwargs.config.loss
    self.assertEqual(loss.image_weight, 1.0)
    self.assertEqual(loss.machine_weight, 0.0)

  def test_combined_smoke_enables_both_distortions(self):
    loss = kodak_machine_smoke.get_config().experiment_kwargs.config.loss
    self.assertEqual(loss.image_weight, 1.0)
    self.assertEqual(loss.machine_weight, 1.0)

  def test_machine_only_smoke_disables_image_distortion(self):
    loss = kodak_machine_only_smoke.get_config().experiment_kwargs.config.loss
    self.assertEqual(loss.image_weight, 0.0)
    self.assertEqual(loss.machine_weight, 1.0)


if __name__ == "__main__":
  unittest.main()
