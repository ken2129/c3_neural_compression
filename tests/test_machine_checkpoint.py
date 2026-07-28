"""Tests objective-safe Phase 3 resumable checkpoints."""

import os
import pickle
import tempfile
import unittest

import jax.numpy as jnp

from c3_neural_compression.configs import kodak_machine_smoke
from c3_neural_compression.experiments import image


class MachineCheckpointTest(unittest.TestCase):

  def _experiment(self, directory):
    config = kodak_machine_smoke.get_config().experiment_kwargs.config
    config.unlock()
    config.checkpointing.enabled = True
    config.checkpointing.resume = True
    config.checkpointing.directory = directory
    config.lock()
    experiment = object.__new__(image.Experiment)
    experiment.config = config
    experiment._wandb_run = None
    experiment._current_input_signature = {
        'sha256': 'input-a',
        'shape': (4, 5, 3),
    }
    return experiment

  def test_matching_objective_signature_resumes(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment = self._experiment(directory)
      experiment._save_noise_checkpoint(
          {'parameter': jnp.array([1.0])},
          {'state': jnp.array([2.0])},
          jnp.array([3, 4], dtype=jnp.uint32),
          1,
      )
      payload = experiment._load_noise_checkpoint()
      self.assertEqual(payload['version'], 3)
      self.assertEqual(payload['next_step'], 1)
      self.assertEqual(
          payload['objective_signature'], experiment._objective_signature()
      )

  def test_changed_objective_is_rejected(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment = self._experiment(directory)
      experiment._save_noise_checkpoint({}, {}, None, 1)
      experiment.config.unlock()
      experiment.config.loss.machine_weight = 2.0
      experiment.config.lock()
      with self.assertRaisesRegex(ValueError, 'objective signature'):
        experiment._load_noise_checkpoint()

  def test_changed_input_is_rejected(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment = self._experiment(directory)
      experiment._save_noise_checkpoint({}, {}, None, 1)
      experiment._current_input_signature = {
          'sha256': 'input-b',
          'shape': (4, 5, 3),
      }
      with self.assertRaisesRegex(ValueError, 'objective signature'):
        experiment._load_noise_checkpoint()

  def test_legacy_checkpoint_is_rejected_for_machine_loss(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment = self._experiment(directory)
      path = os.path.join(directory, 'noise_step_000000001.pkl')
      with open(path, 'wb') as stream:
        pickle.dump({
            'version': 1,
            'phase': 'noise',
            'next_step': 1,
            'num_noise_steps': experiment.config.opt.num_noise_steps,
        }, stream)
      with self.assertRaisesRegex(ValueError, 'Legacy checkpoint'):
        experiment._load_noise_checkpoint()

  def test_legacy_checkpoint_remains_compatible_with_image_only(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment = self._experiment(directory)
      experiment.config.unlock()
      experiment.config.loss.machine_weight = 0.0
      experiment.config.lock()
      path = os.path.join(directory, 'noise_step_000000001.pkl')
      with open(path, 'wb') as stream:
        pickle.dump({
            'version': 1,
            'phase': 'noise',
            'next_step': 1,
            'num_noise_steps': experiment.config.opt.num_noise_steps,
        }, stream)
      self.assertEqual(experiment._load_noise_checkpoint()['version'], 1)


if __name__ == '__main__':
  unittest.main()
