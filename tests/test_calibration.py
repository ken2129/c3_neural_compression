"""Tests for Phase 4 calibration result aggregation."""

import json
from pathlib import Path
import tempfile
import unittest

from c3_neural_compression.evaluation import calibration
from c3_neural_compression.evaluation import coco_detection


class CalibrationTest(unittest.TestCase):

  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.root = Path(self.temporary.name)
    self.annotation = self.root / 'instances.json'
    self.subset = self.root / 'subset.json'
    self.annotation.write_text(json.dumps({
        'images': [{'id': 7}, {'id': 9}],
        'annotations': [
            {'id': 1, 'image_id': 7, 'category_id': 2, 'area': 100},
            {'id': 2, 'image_id': 7, 'category_id': 3, 'area': 2000},
            {'id': 3, 'image_id': 9, 'category_id': 3, 'area': 10000},
        ],
    }), encoding='utf-8')
    self.subset.write_text('[7, 9]', encoding='utf-8')
    self.first = self._condition('first', 0.4, 0.3, 28.0)
    self.second = self._condition('second', 0.8, 0.4, 30.0)

  def tearDown(self):
    self.temporary.cleanup()

  def _condition(self, name, bpp, box_ap, psnr):
    root = self.root / name
    (root / 'coco_detection').mkdir(parents=True)
    summary = {
        'rate_definition': 'entropy_estimated',
        'image_count': 2,
        'estimated_bpp': {
            'total': bpp, 'latents': bpp - 0.1,
            'synthesis_network': 0.05, 'entropy_network': 0.05,
        },
        'experiment_signature': {
            'sha256': name,
            'fields': {
                'rd_weight': 0.001, 'image_weight': 1.0,
                'machine_weight': 0.0, 'code_commit': 'abc',
            },
        },
        'images': [
            {'image_id': 7, 'psnr': psnr},
            {'image_id': 9, 'psnr': psnr + 2},
        ],
    }
    (root / 'phase4_summary.json').write_text(
        json.dumps(summary), encoding='utf-8')
    (root / 'coco_detection' / 'metrics.json').write_text(json.dumps({
        'box_ap': box_ap, 'box_ap50': box_ap + 0.1,
        'box_ap75': box_ap - 0.1,
    }), encoding='utf-8')
    return root

  def test_collects_conditions_and_writes_all_artifacts(self):
    rows = calibration.collect_conditions([
        ('first', self.first), ('second', self.second)])
    self.assertEqual([row['estimated_bpp_total'] for row in rows], [0.4, 0.8])
    self.assertEqual(rows[0]['mean_psnr_db'], 29.0)
    output = self.root / 'output'
    calibration.write_outputs(output, rows)
    for name in ('calibration_summary.json', 'calibration_summary.csv',
                 'bpp_map.svg', 'bpp_psnr.svg'):
      self.assertTrue((output / name).is_file())

  def test_rejects_mismatched_image_ids(self):
    path = self.second / 'phase4_summary.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['images'][1]['image_id'] = 10
    path.write_text(json.dumps(value), encoding='utf-8')
    with self.assertRaisesRegex(
        coco_detection.InputValidationError, 'Image ID sequence mismatch'):
      calibration.collect_conditions([
          ('first', self.first), ('second', self.second)])

  def test_subset_statistics_uses_coco_area_ranges(self):
    result = calibration.subset_statistics(self.annotation, self.subset)
    self.assertEqual(result['annotation_count'], 3)
    self.assertEqual(result['category_count'], 2)
    self.assertEqual(
        result['object_area_counts'], {'small': 1, 'medium': 1, 'large': 1})


if __name__ == '__main__':
  unittest.main()
