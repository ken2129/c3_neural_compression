"""Synthetic correctness tests; these do not measure detector or codec AP."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from PIL import Image
from c3_neural_compression.evaluation import comparison as c
from c3_neural_compression.evaluation import comparison_runner as runner


class ComparisonTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    images = []
    for image_id, height, width in [(7, 3, 4), (9, 5, 2)]:
      filename = f'{image_id}.png'
      Image.new('RGB', (width, height)).save(self.root / filename)
      images.append({'id': image_id, 'height': height, 'width': width, 'file_name': filename})
    c.write_json(self.root / 'annotations.json', {'images': images})
    c.write_json(self.root / 'ids.json', [7, 9])
    self.manifest = c.make_manifest(self.root / 'annotations.json', self.root / 'ids.json', self.root)
    self.rows = [dict(image_id=image_id, status='ok', rate_definition='entropy_estimated', original_hw=[h,w], detector_hw=[h*2,w*2], codec_hw=[256,256], estimated_bits={'y': bits/2,'z':bits/2,'total':bits}) for image_id,h,w,bits in [(7,3,4,12),(9,5,2,20)]]

  def test_weighted_original_denominator(self):
    result = c.aggregate(self.manifest, self.rows, 'transtic')
    self.assertAlmostEqual(result['estimated_bpp'], 32/22)
    self.assertEqual(result['per_image_bpp_mean_auxiliary'], 1.5)
    self.assertEqual(result['native_detector_denominator_bpp_mean_auxiliary'], .375)

  def test_rejects_missing_duplicate_extra_failure(self):
    bad = [self.rows[:1], self.rows + self.rows[:1], self.rows + [dict(self.rows[0], image_id=10)], [dict(self.rows[0], status='failed'), self.rows[1]]]
    for rows in bad:
      with self.subTest(rows=rows), self.assertRaises(ValueError):
        c.aggregate(self.manifest, rows, 'transtic')

  def test_result_order_canonical(self):
    result = c.aggregate(self.manifest, list(reversed(self.rows)), 'transtic')
    self.assertEqual([r['image_id'] for r in result['images']], [7,9])

  def test_bad_rates_dimensions_and_actual_rate(self):
    variants = [dict(rate_definition='actual_bitstream'), dict(original_hw=[4,3]), dict(codec_hw=[0,256]), dict(detector_hw=[True,4]), dict(image_id=True)]
    for fields in variants:
      with self.subTest(fields=fields), self.assertRaises(ValueError):
        c.aggregate(self.manifest, [dict(self.rows[0], **fields), self.rows[1]], 'transtic')
    for bits in ({'y':-1,'z':13,'total':12}, {'y':6,'z':6,'total':13}, {'y':float('nan'),'z':6,'total':12}, {'y':True,'z':11,'total':12}, {'total':12}):
      with self.subTest(bits=bits), self.assertRaises(ValueError):
        c.aggregate(self.manifest, [dict(self.rows[0], estimated_bits=bits), self.rows[1]], 'transtic')

  def test_strict_ids(self):
    for ids in ([7,7], [True], [1.2], ['7'], []):
      with self.subTest(ids=ids), self.assertRaises(ValueError):
        c.image_ids(ids)

  def test_asset_hash_and_dimensions(self):
    c.validate_assets(self.manifest)
    Image.new('RGB', (4,3), color='red').save(self.root / '7.png')
    with self.assertRaisesRegex(ValueError, 'SHA256'):
      c.validate_assets(self.manifest)

  def test_mock_loop_uses_all_ids_even_empty_predictions(self):
    detector = mock.Mock()
    detector.predict.return_value = []
    evaluate = mock.Mock(return_value={'test_only': True})
    by_id = {r['image_id']:r for r in self.rows}
    runner.run_images(self.manifest, 'transtic', lambda image: (None, by_id[image['image_id']]), detector, self.root / 'run', evaluate)
    self.assertEqual(evaluate.call_args.args[2], [7,9])
    self.assertEqual(evaluate.call_args.args[1], [])
    self.assertEqual(c.read_json(self.root / 'run/status.json')['status'], 'complete')

  def test_failed_image_saved_no_summary_or_ap(self):
    detector, evaluate = mock.Mock(), mock.Mock()
    detector.predict.return_value = []
    def reconstruct(image):
      if image['image_id'] == 9:
        raise RuntimeError('synthetic failure')
      return None, self.rows[0]
    with self.assertRaisesRegex(RuntimeError, 'synthetic'):
      runner.run_images(self.manifest, 'transtic', reconstruct, detector, self.root / 'run', evaluate)
    evaluate.assert_not_called()
    self.assertFalse((self.root / 'run/rate_summary.json').exists())
    self.assertEqual(c.read_json(self.root / 'run/per_image.json')[-1]['status'], 'failed')

  def test_wrong_prediction_image_rejected(self):
    detector = mock.Mock()
    detector.predict.return_value = [{'image_id':9}]
    with self.assertRaisesRegex(ValueError, 'another image'):
      runner.run_images(self.manifest, 'transtic', lambda image: (None,self.rows[0]), detector, self.root / 'run', mock.Mock())

  def test_write_nan_rejected(self):
    with self.assertRaises(ValueError):
      c.write_json(self.root / 'nan.json', {'rate':float('nan')})

  def _c3_attempt(self, root, mixed=False):
    from c3_neural_compression.evaluation.comparison_attempt import ComparisonAttempt
    attempt = ComparisonAttempt.start(root, self.manifest['image_ids'])
    for idx, row in enumerate(self.manifest['images']):
      datum = root / f'datum_{idx:05d}'
      datum.mkdir()
      Image.new('RGB', tuple(reversed(row['original_hw']))).save(datum / 'reconstruction.png')
      c.write_json(datum / 'config.json', {'test_only':True})
      c.write_json(datum / 'metrics.json', {
          'attempt_id':attempt.attempt_id, 'datum_index':idx,
          'datum_metadata':{'image_id':row['image_id'], 'file_name':row['file_name'], 'original_file_sha256':row['sha256']},
          'experiment_signature':{'sha256':'different' if mixed and idx else 'fixed', 'fields':{'machine_weight':0}},
          'estimated_bits':{'latents':4,'synthesis_network':3,'entropy_network':5,'total':12}})
      attempt.record_output(idx,row['image_id'])
    attempt.complete()

  def test_c3_discovery_rejects_mixed_signature_and_duplicates(self):
    root = self.root / 'c3'
    self._c3_attempt(root)
    found, _ = c.discover_c3(self.manifest, [root], 'c3_image')
    self.assertEqual(set(found), {7,9})
    with self.assertRaisesRegex(ValueError, 'duplicate'):
      c.discover_c3(self.manifest, [root,root], 'c3_image')
    with self.assertRaisesRegex(ValueError, 'objective'):
      c.discover_c3(self.manifest, [root], 'c3_combined')
    mixed = self.root / 'mixed'
    self._c3_attempt(mixed, mixed=True)
    with self.assertRaisesRegex(ValueError, 'Mixed'):
      c.discover_c3(self.manifest,[mixed],'c3_image')


if __name__ == '__main__':
  unittest.main()
