"""CPU-only ledger tests and source-extracted Experiment integration tests.

The actual Experiment methods are AST-extracted to test preflight and artifact
writing without importing unavailable JAX/Haiku. These are not C3 training or
GPU validation; reconstruction/JAX values in writer tests are small CPU fakes.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from c3_neural_compression.evaluation import comparison_attempt as attempts


def write_output(attempt, index, image_id, attempt_id=None):
  directory = attempt.root / f'datum_{index:05d}'
  directory.mkdir()
  metrics = {
      'attempt_id': attempt.attempt_id if attempt_id is None else attempt_id,
      'datum_index': index, 'datum_metadata': {'image_id': image_id},
  }
  (directory / 'metrics.json').write_text(json.dumps(metrics), encoding='utf-8')
  (directory / 'config.json').write_text(
      json.dumps(attempt.ledger.get('config_snapshot', {})), encoding='utf-8')
  (directory / 'reconstruction.png').write_bytes(b'fake PNG for ledger hashing')
  attempt.record_output(index, image_id)


def read_ledger(root):
  return json.loads((Path(root) / attempts.LEDGER_NAME).read_text(encoding='utf-8'))


class ComparisonAttemptTest(unittest.TestCase):

  def test_nonempty_root_is_rejected_without_changing_any_bytes(self):
    for stale_name in ('datum_00000', 'checkpoints', attempts.LEDGER_NAME, '.hidden'):
      with self.subTest(stale=stale_name), tempfile.TemporaryDirectory() as root:
        stale = Path(root) / stale_name
        stale.write_bytes(b'old attempt must not be touched')
        with self.assertRaisesRegex(ValueError, 'must be empty'):
          attempts.ComparisonAttempt.start(root, [1])
        self.assertEqual(stale.read_bytes(), b'old attempt must not be touched')
        self.assertEqual(list(Path(root).iterdir()), [stale])

  def test_competing_starts_have_exactly_one_owner(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory) / 'run'

      def contender(_):
        try:
          return attempts.ComparisonAttempt.start(root, [1])
        except ValueError:
          return None

      with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [value for value in pool.map(contender, range(24)) if value]
      self.assertEqual(len(winners), 1)
      self.assertEqual(read_ledger(root)['attempt_id'], winners[0].attempt_id)
      self.assertEqual(read_ledger(root)['status'], 'running')

  def test_completed_attempt_records_exact_order_and_all_artifact_hashes(self):
    with tempfile.TemporaryDirectory() as root:
      attempt = attempts.ComparisonAttempt.start(root, [7, 2])
      with self.assertRaisesRegex(ValueError, 'status completed'):
        attempts.validate_completed_attempt(root)
      write_output(attempt, 0, 7)
      with self.assertRaisesRegex(ValueError, 'exactly match'):
        attempt.complete()
      write_output(attempt, 1, 2)
      (Path(root) / 'checkpoints' / 'datum_00000').mkdir(parents=True)
      attempt.complete()
      ledger = attempts.validate_completed_attempt(root)
      self.assertEqual(ledger['image_ids'], [7, 2])
      self.assertEqual([row['image_id'] for row in ledger['outputs']], [7, 2])
      self.assertTrue(ledger['finished_at'])
      for row in ledger['outputs']:
        for kind in ('metrics', 'config', 'reconstruction'):
          data = (Path(root) / row[f'{kind}_path']).read_bytes()
          self.assertEqual(row[f'{kind}_sha256'], hashlib.sha256(data).hexdigest())
      with self.assertRaisesRegex(ValueError, 'must be empty'):
        attempts.ComparisonAttempt.start(root, [7, 2])

  def test_failure_keeps_partial_output_and_cannot_be_completed_or_reused(self):
    with tempfile.TemporaryDirectory() as root:
      attempt = attempts.ComparisonAttempt.start(root, [1, 2])
      write_output(attempt, 0, 1)
      attempt.fail(RuntimeError('second image failed'))
      ledger = read_ledger(root)
      self.assertEqual(ledger['status'], 'failed')
      self.assertEqual(ledger['failure']['type'], 'RuntimeError')
      self.assertEqual(len(ledger['outputs']), 1)
      with self.assertRaisesRegex(ValueError, 'status completed'):
        attempts.validate_completed_attempt(root)
      with self.assertRaisesRegex(ValueError, 'running ledger'):
        attempt.complete()
      with self.assertRaisesRegex(ValueError, 'must be empty'):
        attempts.ComparisonAttempt.start(root, [1, 2])

  def test_changed_file_hashes_and_added_outputs_fail_closed(self):
    for filename in ('metrics.json', 'config.json', 'reconstruction.png', 'extra'):
      with self.subTest(filename=filename), tempfile.TemporaryDirectory() as root:
        attempt = attempts.ComparisonAttempt.start(root, [1])
        write_output(attempt, 0, 1)
        attempt.complete()
        (Path(root) / 'datum_00000' / filename).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA256|artifacts'):
          attempts.validate_completed_attempt(root)
    with tempfile.TemporaryDirectory() as root:
      attempt = attempts.ComparisonAttempt.start(root, [1])
      write_output(attempt, 0, 1)
      attempt.complete()
      (Path(root) / 'datum_00001').mkdir()
      with self.assertRaisesRegex(ValueError, 'files in comparison output root'):
        attempts.validate_completed_attempt(root)

  def test_foreign_attempt_metrics_and_duplicate_registration_are_rejected(self):
    with tempfile.TemporaryDirectory() as root:
      attempt = attempts.ComparisonAttempt.start(root, [1])
      with self.assertRaisesRegex(ValueError, 'attempt_id'):
        write_output(attempt, 0, 1, attempt_id='b' * 32)
    with tempfile.TemporaryDirectory() as root:
      attempt = attempts.ComparisonAttempt.start(root, [1])
      write_output(attempt, 0, 1)
      with self.assertRaisesRegex(ValueError, 'Unexpected/duplicate'):
        attempt.record_output(0, 1)

  def test_metrics_identity_is_checked_even_when_hashes_agree(self):
    with tempfile.TemporaryDirectory() as root:
      attempt = attempts.ComparisonAttempt.start(root, [1])
      write_output(attempt, 0, 1)
      attempt.complete()
      path = Path(root) / 'datum_00000' / 'metrics.json'
      metrics = json.loads(path.read_text(encoding='utf-8'))
      metrics['attempt_id'] = '0' * 32
      path.write_text(json.dumps(metrics), encoding='utf-8')
      ledger = read_ledger(root)
      ledger['outputs'][0]['metrics_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
      (Path(root) / attempts.LEDGER_NAME).write_text(json.dumps(ledger), encoding='utf-8')
      with self.assertRaisesRegex(ValueError, 'attempt_id'):
        attempts.validate_completed_attempt(root)

  def test_missing_ledger_and_symlinked_artifacts_are_rejected(self):
    with tempfile.TemporaryDirectory() as root:
      with self.assertRaisesRegex(ValueError, 'Cannot read comparison artifact'):
        attempts.validate_completed_attempt(root)
      attempt = attempts.ComparisonAttempt.start(root, [1])
      write_output(attempt, 0, 1)
      attempt.complete()
      reconstruction = Path(root) / 'datum_00000' / 'reconstruction.png'
      data = reconstruction.read_bytes()
      reconstruction.unlink()
      with tempfile.NamedTemporaryFile() as target:
        target.write(data)
        target.flush()
        reconstruction.symlink_to(target.name)
        with self.assertRaisesRegex(ValueError, 'invalid comparison artifact'):
          attempts.validate_completed_attempt(root)

  def test_invalid_ids_are_rejected_before_creating_root(self):
    for ids in ([], [1, 1], [True], [-1], [1.0], None):
      with self.subTest(ids=ids), tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'run'
        with self.assertRaisesRegex(ValueError, 'image_ids'):
          attempts.ComparisonAttempt.start(root, ids)
        self.assertFalse(root.exists())


def extract_experiment_method(name, globals_):
  """Compiles the real method body without importing the JAX experiment module."""
  path = Path(__file__).parents[1] / 'experiments' / 'image.py'
  module = ast.parse(path.read_text(encoding='utf-8'))
  experiment = next(node for node in module.body
                    if isinstance(node, ast.ClassDef) and node.name == 'Experiment')
  method = next(node for node in experiment.body
                if isinstance(node, ast.FunctionDef) and node.name == name)
  namespace = dict(globals_)
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
  return namespace[name]


class AttrDict(dict):
  __getattr__ = dict.__getitem__

  def to_dict(self):
    def convert(value):
      if isinstance(value, SimpleNamespace):
        value = vars(value)
      if isinstance(value, dict):
        return {key: convert(item) for key, item in value.items()}
      return value
    return convert(self)


class ExperimentGuardTest(unittest.TestCase):

  def experiment(self, directory, ids=(1,), enabled=True):
    root = Path(directory) / 'outputs'
    subset = Path(directory) / 'ids.json'
    subset.write_text(json.dumps(list(ids)), encoding='utf-8')
    config = AttrDict(
        output_dir=str(root), comparison_guard={'enabled': enabled},
        random_seed=20261004,
        dataset=SimpleNamespace(name='coco2017', subset_ids_file=str(subset),
                                skip_examples=0, num_examples=None),
        checkpointing=SimpleNamespace(resume=False),
    )
    step = extract_experiment_method('step', {
        'comparison_attempt': attempts, 'logging': logging,
    })
    fake_class = type('FakeExperiment', (), {'step': step})
    experiment = fake_class()
    experiment.config = config
    return experiment, root

  def call(self, experiment):
    return experiment.step(global_step=0, rng='fake rng', writer=None)

  def test_nonempty_root_rejected_before_training_method_is_called(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment, root = self.experiment(directory)
      root.mkdir()
      (root / 'stale').write_text('untouched', encoding='utf-8')
      experiment._step_impl = mock.Mock()
      with self.assertRaisesRegex(ValueError, 'must be empty'):
        self.call(experiment)
      experiment._step_impl.assert_not_called()
      self.assertEqual((root / 'stale').read_text(), 'untouched')

  def test_general_image_resume_bypasses_comparison_guard(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment, root = self.experiment(directory, enabled=False)
      experiment.config.checkpointing.resume = True
      del experiment.config['comparison_guard']  # Existing configs lack this key.
      root.mkdir()
      (root / 'old_checkpoint').write_text('preserved', encoding='utf-8')
      experiment._step_impl = mock.Mock(return_value={'general': 'resumed'})
      self.assertEqual(self.call(experiment), {'general': 'resumed'})
      experiment._step_impl.assert_called_once_with(global_step=0, rng='fake rng', writer=None)
      self.assertFalse((root / attempts.LEDGER_NAME).exists())

  def test_comparison_resume_is_rejected_before_training(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment, root = self.experiment(directory)
      experiment.config.checkpointing.resume = True
      experiment._step_impl = mock.Mock()
      with self.assertRaisesRegex(ValueError, 'cannot resume'):
        self.call(experiment)
      experiment._step_impl.assert_not_called()
      self.assertFalse(root.exists())

  def test_failure_before_first_output_preserves_starting_config_and_seed(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment, root = self.experiment(directory)
      experiment._step_impl = mock.Mock(side_effect=RuntimeError('first fit failed'))
      with self.assertRaisesRegex(RuntimeError, 'first fit failed'):
        self.call(experiment)
      ledger = read_ledger(root)
      self.assertEqual(ledger['status'], 'failed')
      self.assertEqual(ledger['outputs'], [])
      self.assertEqual(ledger['random_seed'], 20261004)
      self.assertEqual(ledger['config_snapshot'], experiment.config.to_dict())
      canonical = json.dumps(ledger['config_snapshot'], sort_keys=True, separators=(',', ':'))
      self.assertEqual(ledger['config_sha256'], hashlib.sha256(canonical.encode()).hexdigest())

  def test_all_outputs_then_success_becomes_completed(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment, root = self.experiment(directory, ids=(1, 2))

      def body(**_):
        self.assertEqual(read_ledger(root)['status'], 'running')
        write_output(experiment._comparison_attempt, 0, 1)
        write_output(experiment._comparison_attempt, 1, 2)
        self.assertEqual(read_ledger(root)['status'], 'running')
        return {'all': 'images'}

      experiment._step_impl = body
      self.assertEqual(self.call(experiment), {'all': 'images'})
      self.assertEqual(attempts.validate_completed_attempt(root)['status'], 'completed')
      self.assertIsNone(experiment._comparison_attempt)

  def test_completed_full_run_cannot_be_partially_overwritten_by_a_rerun(self):
    with tempfile.TemporaryDirectory() as directory:
      experiment, root = self.experiment(directory, ids=(1, 2))

      def full_run(**_):
        write_output(experiment._comparison_attempt, 0, 1)
        write_output(experiment._comparison_attempt, 1, 2)
        return {}

      experiment._step_impl = full_run
      self.call(experiment)
      original_files = {path.relative_to(root): path.read_bytes()
                        for path in root.rglob('*') if path.is_file()}
      # The second attempt may have the same image IDs or configuration. It
      # must fail before touching even the first old datum, regardless.
      experiment._step_impl = mock.Mock(side_effect=RuntimeError('partial rerun'))
      with self.assertRaisesRegex(ValueError, 'must be empty'):
        self.call(experiment)
      experiment._step_impl.assert_not_called()
      self.assertEqual(original_files, {path.relative_to(root): path.read_bytes()
                                       for path in root.rglob('*') if path.is_file()})
      self.assertEqual(attempts.validate_completed_attempt(root)['status'], 'completed')

  def test_error_or_interrupt_after_partial_output_is_persisted_as_failed(self):
    for error in (RuntimeError('fit failed'), KeyboardInterrupt()):
      with self.subTest(error=type(error)), tempfile.TemporaryDirectory() as directory:
        experiment, root = self.experiment(directory, ids=(1, 2))

        def body(**_):
          write_output(experiment._comparison_attempt, 0, 1)
          raise error

        experiment._step_impl = body
        with self.assertRaises(type(error)):
          self.call(experiment)
        ledger = read_ledger(root)
        self.assertEqual(ledger['status'], 'failed')
        self.assertEqual(ledger['failure']['type'], type(error).__name__)
        self.assertEqual(len(ledger['outputs']), 1)

  def test_truncated_success_and_postwrite_failure_never_become_completed(self):
    for full in (False, True):
      with self.subTest(full=full), tempfile.TemporaryDirectory() as directory:
        experiment, root = self.experiment(directory, ids=(1, 2))

        def body(**_):
          write_output(experiment._comparison_attempt, 0, 1)
          if full:
            write_output(experiment._comparison_attempt, 1, 2)
            raise RuntimeError('error after final image')
          return {}

        experiment._step_impl = body
        with self.assertRaises((RuntimeError, ValueError)):
          self.call(experiment)
        self.assertEqual(read_ledger(root)['status'], 'failed')

  def test_real_artifact_writer_attaches_attempt_id_and_registers_hashes(self):
    import numpy as np
    from PIL import Image
    from c3_neural_compression.configs import coco2017_comparison

    with tempfile.TemporaryDirectory() as directory:
      env = {key: directory for key in (
          'C3_COCO_ROOT', 'C3_COCO_ANNOTATIONS', 'C3_SUBSET_IDS', 'C3_OUTPUT_ROOT')}
      with mock.patch.dict(os.environ, env):
        config = coco2017_comparison.get_config().experiment_kwargs.config
      self.assertTrue(config.comparison_guard.enabled)
      fake_jax = SimpleNamespace(device_get=lambda value: value,
                                 __version__='test fake', devices=lambda: [])
      writer = extract_experiment_method('_save_datum_outputs', {
          'os': os, 'np': np, 'jax': fake_jax, 'Image': Image,
          'subprocess': subprocess, 'json': json, 'hashlib': hashlib,
          'sys': sys, 'python_platform': platform,
          '__file__': str(Path(__file__).parents[1] / 'experiments' / 'image.py'),
      })
      fake_class = type('FakeArtifactWriter', (), {'save': writer})
      experiment = fake_class()
      experiment.config = config
      attempt = attempts.ComparisonAttempt.start(
          config.output_dir, [9], config_snapshot=config.to_dict())
      experiment._comparison_attempt = attempt
      experiment.reconstruct = lambda params, shape: np.zeros((*shape, 3), dtype=np.float32)
      experiment._num_pixels = lambda shape: int(np.prod(shape))
      metrics = dict(rate=1., synthesis=2., entropy=3., psnr=30., ssim=.9,
                     q_step_weight=.1, q_step_bias=.1)
      metrics.update({f'rate_grid_{index}': 0.1 for index in range(config.model.latents.num_grids)})
      experiment.save(0, np.zeros((3, 4, 3)), {}, metrics, {}, {},
                      datum_metadata={'image_id': 9, 'original_file_sha256': 'a' * 64})
      attempt.complete()
      ledger = attempts.validate_completed_attempt(config.output_dir)
      saved_metrics = json.loads((Path(config.output_dir) / ledger['outputs'][0]['metrics_path']).read_text())
      self.assertEqual(saved_metrics['attempt_id'], attempt.attempt_id)
      self.assertEqual(saved_metrics['datum_metadata']['original_file_sha256'], 'a' * 64)
      with Image.open(Path(config.output_dir) / ledger['outputs'][0]['reconstruction_path']) as image:
        self.assertEqual(image.size, (4, 3))


if __name__ == '__main__':
  unittest.main()
