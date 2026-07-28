"""Tests for Phase 4 reconstruction preparation and rate aggregation."""

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from c3_neural_compression.evaluation import coco_detection
from c3_neural_compression.evaluation import phase4


class Phase4Test(unittest.TestCase):

  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.root = Path(self.temporary.name)
    self.annotation = self.root / 'instances.json'
    self.subset = self.root / 'subset.json'
    self.c3_root = self.root / 'c3'
    self.image_path_map = self.root / 'image_paths.json'
    self.annotation.write_text(json.dumps({'images': [
        {'id': 7, 'file_name': '000000000007.jpg', 'width': 4, 'height': 3},
        {'id': 9, 'file_name': '000000000009.jpg', 'width': 2, 'height': 5},
    ]}), encoding='utf-8')
    self.subset.write_text('[7, 9]', encoding='utf-8')
    self._write_datum(0, 7, '000000000007.jpg', (4, 3), 12.0)
    self._write_datum(1, 9, '000000000009.jpg', (2, 5), 20.0)

  def tearDown(self):
    self.temporary.cleanup()

  def _write_datum(self, index, image_id, file_name, size, total_bits):
    directory = self.c3_root / f'datum_{index:05d}'
    directory.mkdir(parents=True)
    Image.new('RGB', size).save(directory / 'reconstruction.png')
    (directory / 'metrics.json').write_text(json.dumps({
        'datum_index': index,
        'datum_metadata': {'image_id': image_id, 'file_name': file_name},
        'estimated_bits': {
            'latents': total_bits / 2,
            'synthesis_network': total_bits / 4,
            'entropy_network': total_bits / 4,
            'total': total_bits,
        },
        'psnr_quantized': 10.0 + index,
    }), encoding='utf-8')

  def test_prepares_mapping_and_pixel_weighted_dataset_bpp(self):
    result = phase4.prepare_c3_outputs(
        self.c3_root, self.annotation, self.subset, self.image_path_map
    )
    self.assertEqual(result['image_count'], 2)
    self.assertEqual(result['total_pixels'], 22)
    self.assertAlmostEqual(result['estimated_bpp']['total'], 32 / 22)
    self.assertNotAlmostEqual(
        result['estimated_bpp']['total'], result['per_image_bpp_mean_auxiliary']
    )
    mapping = json.loads(self.image_path_map.read_text(encoding='utf-8'))
    self.assertEqual([row['image_id'] for row in mapping['images']], [7, 9])
    self.assertTrue(mapping['images'][0]['path'].endswith('reconstruction.png'))

  def test_rejects_duplicate_output_image_id(self):
    metrics_path = self.c3_root / 'datum_00000' / 'metrics.json'
    metrics = json.loads(metrics_path.read_text(encoding='utf-8'))
    metrics['datum_metadata']['image_id'] = 9
    metrics_path.write_text(json.dumps(metrics), encoding='utf-8')
    with self.assertRaisesRegex(
        coco_detection.InputValidationError, 'Duplicate C3 output'
    ):
      phase4.prepare_c3_outputs(
          self.c3_root, self.annotation, self.subset, self.image_path_map
      )

  def test_discovers_chunk_output_by_image_id(self):
    self.subset.write_text('[9]', encoding='utf-8')
    result = phase4.prepare_c3_outputs(
        self.c3_root, self.annotation, self.subset, self.image_path_map
    )
    self.assertEqual([row['image_id'] for row in result['images']], [9])
    mapping = json.loads(self.image_path_map.read_text(encoding='utf-8'))
    self.assertEqual(mapping['images'][0]['image_id'], 9)

  def _set_bits(self, **updates):
    path = self.c3_root / 'datum_00000' / 'metrics.json'
    metrics = json.loads(path.read_text(encoding='utf-8'))
    metrics['estimated_bits'].update(updates)
    path.write_text(json.dumps(metrics), encoding='utf-8')

  def test_rejects_nonfinite_rate(self):
    for value in (float('nan'), float('inf')):
      with self.subTest(value=value):
        self._set_bits(total=value)
        with self.assertRaisesRegex(
            coco_detection.InputValidationError, 'Non-finite estimated_bits'
        ):
          phase4.prepare_c3_outputs(
              self.c3_root, self.annotation, self.subset, self.image_path_map
          )
        self._set_bits(total=12.0)

  def test_rejects_negative_rate(self):
    self._set_bits(latents=-1.0)
    with self.assertRaisesRegex(
        coco_detection.InputValidationError, 'Negative estimated_bits'
    ):
      phase4.prepare_c3_outputs(
          self.c3_root, self.annotation, self.subset, self.image_path_map
      )

  def test_rejects_rate_breakdown_mismatch(self):
    self._set_bits(total=13.0)
    with self.assertRaisesRegex(
        coco_detection.InputValidationError, 'Rate breakdown mismatch'
    ):
      phase4.prepare_c3_outputs(
          self.c3_root, self.annotation, self.subset, self.image_path_map
      )

  def test_rejects_empty_manifest(self):
    self.subset.write_text('[]', encoding='utf-8')
    with self.assertRaisesRegex(
        coco_detection.InputValidationError, 'manifest is empty'
    ):
      phase4.prepare_c3_outputs(
          self.c3_root, self.annotation, self.subset, self.image_path_map
      )


if __name__ == '__main__':
  unittest.main()
