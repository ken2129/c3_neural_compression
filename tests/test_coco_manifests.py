"""Tests for deterministic disjoint COCO manifests."""

import json
from pathlib import Path
import tempfile
import unittest

from c3_neural_compression.evaluation import coco_detection
from c3_neural_compression.evaluation import coco_manifests


class CocoManifestsTest(unittest.TestCase):

  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.root = Path(self.temporary.name)
    self.annotation = self.root / 'instances.json'
    self.annotation.write_text(json.dumps({
        'images': [{'id': image_id} for image_id in range(1, 11)]
    }), encoding='utf-8')
    self.smoke = self.root / 'smoke.json'
    self.calibration = self.root / 'calibration.json'
    self.main = self.root / 'main.json'
    self.smoke.write_text('[1, 2]', encoding='utf-8')

  def tearDown(self):
    self.temporary.cleanup()

  def test_generation_is_deterministic_and_disjoint(self):
    first = coco_manifests.generate_manifests(
        self.annotation, self.smoke, self.calibration, self.main, 3, 3, 3
    )
    second = coco_manifests.generate_manifests(
        self.annotation, self.smoke, self.calibration, self.main, 3, 3, 3
    )
    self.assertEqual(first, second)
    self.assertFalse(set(first[0]) & set(first[1]))
    self.assertFalse(set(first[0] + first[1]) & {1, 2})

  def test_validation_rejects_overlap(self):
    self.calibration.write_text('[2, 3]', encoding='utf-8')
    with self.assertRaisesRegex(
        coco_detection.InputValidationError, 'overlaps manifests'
    ):
      coco_manifests.validate_disjoint_manifests(
          [self.smoke, self.calibration], self.annotation
      )
