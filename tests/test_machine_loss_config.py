"""Tests for Phase 3 image/machine objective modes."""

import unittest
from types import SimpleNamespace

from c3_neural_compression.configs import kodak
from c3_neural_compression.configs import kodak_image_only_pilot
from c3_neural_compression.configs import kodak_machine_only_pilot
from c3_neural_compression.configs import kodak_machine_only_smoke
from c3_neural_compression.configs import kodak_machine_pilot
from c3_neural_compression.configs import kodak_machine_smoke
from c3_neural_compression.configs import kodak_phase3_regression_smoke
from c3_neural_compression.experiments import image


class MachineLossConfigTest(unittest.TestCase):

  @staticmethod
  def _experiment(image_weight, machine_weight, rd_weight=0.25):
    experiment = object.__new__(image.Experiment)
    experiment.config = SimpleNamespace(
        loss=SimpleNamespace(
            image_weight=image_weight,
            machine_weight=machine_weight,
            rd_weight=rd_weight,
        )
    )
    return experiment

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

  def test_image_only_selection_preserves_original_objective(self):
    experiment = self._experiment(image_weight=99.0, machine_weight=0.0)
    value = experiment._objective_from_metrics(
        {'distortion': 2.0, 'rate': 4.0}, num_pixels=2, model_rate=2.0
    )
    self.assertEqual(value, 2.75)

  def test_combined_selection_includes_configured_machine_distortion(self):
    experiment = self._experiment(image_weight=2.0, machine_weight=3.0)
    value = experiment._objective_from_metrics(
        {'distortion': 2.0, 'rate': 4.0},
        num_pixels=2,
        model_rate=2.0,
        machine_distortion=5.0,
    )
    self.assertEqual(value, 19.75)

  def test_machine_selection_requires_machine_distortion(self):
    experiment = self._experiment(image_weight=1.0, machine_weight=1.0)
    with self.assertRaisesRegex(ValueError, 'machine_distortion'):
      experiment._objective_from_metrics(
          {'distortion': 2.0, 'rate': 4.0}, num_pixels=2
      )

  def test_pilot_has_persistent_tracking_and_checkpoints(self):
    config = kodak_machine_pilot.get_config().experiment_kwargs.config
    self.assertEqual(config.opt.num_noise_steps, 500)
    self.assertEqual(config.opt.max_num_ste_steps, 50)
    self.assertEqual(config.loss.machine_weight, 0.1)
    self.assertAlmostEqual(sum(config.loss.machine.feature_layer_weights), 1.0)
    self.assertTrue(config.checkpointing.enabled)
    self.assertEqual(config.checkpointing.save_every_steps, 50)
    self.assertTrue(config.tracking.enabled)

  def test_comparison_pilots_share_schedule_but_isolate_objectives(self):
    image = kodak_image_only_pilot.get_config().experiment_kwargs.config
    machine = kodak_machine_only_pilot.get_config().experiment_kwargs.config
    self.assertEqual(image.opt.num_noise_steps, machine.opt.num_noise_steps)
    self.assertEqual(image.opt.max_num_ste_steps, machine.opt.max_num_ste_steps)
    self.assertEqual((image.loss.image_weight, image.loss.machine_weight), (1, 0))
    self.assertEqual(
        (machine.loss.image_weight, machine.loss.machine_weight), (0, 0.1)
    )
    self.assertNotEqual(image.output_dir, machine.output_dir)
    self.assertNotEqual(
        image.checkpointing.directory, machine.checkpointing.directory
    )
    self.assertNotEqual(image.tracking.run_id, machine.tracking.run_id)

  def test_regression_smoke_uses_phase1_schedule_without_machine_loss(self):
    config = (
        kodak_phase3_regression_smoke.get_config().experiment_kwargs.config
    )
    self.assertEqual(config.opt.num_noise_steps, 100)
    self.assertEqual(config.opt.max_num_ste_steps, 10)
    self.assertEqual(config.loss.image_weight, 1.0)
    self.assertEqual(config.loss.machine_weight, 0.0)
    self.assertFalse(config.checkpointing.enabled)


if __name__ == "__main__":
  unittest.main()
