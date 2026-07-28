"""Synthetic tests: no weights, detector inference, COCO AP, or GPU."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from PIL import Image
from c3_neural_compression.evaluation import coco_detection

class FakeTensor:
  def __init__(self, value): self.value = value
  def detach(self): return self
  def cpu(self): return self
  def tolist(self): return self.value

class CocoDetectionTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.root = Path(self.temp.name)
    self.images = self.root / "images"
    self.images.mkdir()
    Image.new("RGB", (8, 6), "white").save(self.images / "one.png")
    self.annotation = self.root / "instances.json"
    self.write_annotation([{"id": 7, "file_name": "one.png",
                            "width": 8, "height": 6}])
  def tearDown(self): self.temp.cleanup()
  def write_annotation(self, images):
    self.annotation.write_text(json.dumps(
        {"images": images, "annotations": [], "categories": []}),
        encoding="utf-8")
  def test_preserves_image_id(self):
    records, _ = coco_detection.build_image_records(self.images, self.annotation)
    self.assertEqual((records[0].image_id, records[0].path),
                     (7, self.images / "one.png"))
  def test_missing_image(self):
    (self.images / "one.png").unlink()
    with self.assertRaisesRegex(coco_detection.InputValidationError, "image ID 7"):
      coco_detection.build_image_records(self.images, self.annotation)
  def test_duplicate_id(self):
    self.write_annotation([{"id": 7, "file_name": "one.png", "width": 8, "height": 6},
                           {"id": 7, "file_name": "two.png", "width": 8, "height": 6}])
    with self.assertRaisesRegex(coco_detection.InputValidationError, "Duplicate"):
      coco_detection.build_image_records(self.images, self.annotation)
  def test_duplicate_file_mapping(self):
    self.write_annotation([{"id": 7, "file_name": "one.png", "width": 8, "height": 6},
                           {"id": 8, "file_name": "one.png", "width": 8, "height": 6}])
    with self.assertRaisesRegex(coco_detection.InputValidationError, "map to one file"):
      coco_detection.build_image_records(self.images, self.annotation)
  def test_size_mismatch(self):
    self.write_annotation([{"id": 7, "file_name": "one.png", "width": 9, "height": 6}])
    with self.assertRaisesRegex(coco_detection.InputValidationError, "Size mismatch"):
      coco_detection.build_image_records(self.images, self.annotation)
  def test_duplicate_subset(self):
    path = self.root / "ids.json"
    path.write_text("[7, 7]", encoding="utf-8")
    with self.assertRaisesRegex(coco_detection.InputValidationError, "Duplicate"):
      coco_detection.load_subset_ids(path)
  def test_unthresholded_xywh_result(self):
    result = coco_detection.prediction_to_coco(7, {
        "boxes": FakeTensor([[1., 2., 5., 8.]]),
        "labels": FakeTensor([3]), "scores": FakeTensor([.01])})
    self.assertEqual(result, [{"image_id": 7, "category_id": 3,
                               "bbox": [1., 2., 4., 6.], "score": .01}])
  def test_detector_is_frozen_and_eval(self):
    model, weights = mock.Mock(), mock.Mock()
    model.training = False
    model.parameters.return_value = [mock.Mock(requires_grad=False)]
    model.to.return_value = model
    detection = mock.Mock(
        FasterRCNN_ResNet50_FPN_Weights=mock.Mock(COCO_V1=weights),
        fasterrcnn_resnet50_fpn=mock.Mock(return_value=model))
    with mock.patch.dict("sys.modules", {
        "torchvision": mock.Mock(__version__="test"),
        "torchvision.models": mock.Mock(),
        "torchvision.models.detection": detection}):
      result, _, _ = coco_detection.create_frozen_detector("cpu")
    self.assertIs(result, model)
    model.eval.assert_called_once_with()
    model.requires_grad_.assert_called_once_with(False)

  def test_write_json(self):
    path = self.root / "output.json"
    coco_detection.write_json(path, {"value": 3})
    self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 3})
    self.assertFalse((self.root / "output.json.tmp").exists())

  def test_metric_names_match_coco_stats_order(self):
    ground_truth = mock.Mock()
    ground_truth.loadRes.return_value = mock.Mock()
    ground_truth.getImgIds.return_value = [7]
    ground_truth.getAnnIds.return_value = [1, 2]
    evaluator = mock.Mock()
    evaluator.stats = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    evaluator.params = mock.Mock()
    coco_module = mock.Mock(COCO=mock.Mock(return_value=ground_truth))
    eval_module = mock.Mock(COCOeval=mock.Mock(return_value=evaluator))
    with mock.patch.dict("sys.modules", {
        "pycocotools.coco": coco_module,
        "pycocotools.cocoeval": eval_module,
    }):
      metrics = coco_detection.evaluate_coco(
          self.annotation, [{"score": 0.1}], [7])
    self.assertEqual(
        {name: metrics[name] for name in coco_detection.METRICS},
        dict(zip(coco_detection.METRICS, evaluator.stats)),
    )
    self.assertFalse(metrics['empty_predictions'])
    self.assertTrue(metrics['official_cocoeval_executed'])
    self.assertEqual(metrics['ground_truth_annotation_count'], 2)
    self.assertEqual(evaluator.params.imgIds, [7])
    evaluator.evaluate.assert_called_once_with()
    evaluator.accumulate.assert_called_once_with()
    evaluator.summarize.assert_called_once_with()

  def test_empty_predictions_report_zero_ap(self):
    ground_truth = mock.Mock()
    ground_truth.getImgIds.return_value = [7]
    ground_truth.getAnnIds.return_value = [1, 2, 3]
    coco_module = mock.Mock(COCO=mock.Mock(return_value=ground_truth))
    with mock.patch.dict('sys.modules', {'pycocotools.coco': coco_module}):
      metrics = coco_detection.evaluate_coco(self.annotation, [], [7])
    self.assertEqual(
        {name: metrics[name] for name in coco_detection.METRICS},
        {name: 0.0 for name in coco_detection.METRICS},
    )
    self.assertTrue(metrics['empty_predictions'])
    self.assertFalse(metrics['official_cocoeval_executed'])
    self.assertEqual(metrics['ground_truth_annotation_count'], 3)

  def test_empty_predictions_still_reject_missing_annotations(self):
    with self.assertRaises(FileNotFoundError):
      coco_detection.evaluate_coco(self.root / 'missing.json', [], [7])

  def test_empty_predictions_reject_unknown_image_id(self):
    ground_truth = mock.Mock()
    ground_truth.getImgIds.return_value = [7]
    coco_module = mock.Mock(COCO=mock.Mock(return_value=ground_truth))
    with mock.patch.dict('sys.modules', {'pycocotools.coco': coco_module}):
      with self.assertRaisesRegex(
          coco_detection.InputValidationError, 'absent from COCO annotations'
      ):
        coco_detection.evaluate_coco(self.annotation, [], [9])

  def test_detector_config_records_preprocessing(self):
    model = mock.Mock(training=False)
    model.parameters.return_value = [mock.Mock(requires_grad=False)]
    model.transform.min_size = (800,)
    model.transform.max_size = 1333
    model.transform.image_mean = [0.1, 0.2, 0.3]
    model.transform.image_std = [0.4, 0.5, 0.6]
    model.transform.size_divisible = 32
    weights = mock.Mock(url="https://example.invalid/weights.pth", meta={})
    weights.transforms.return_value = mock.Mock()
    config = coco_detection.detector_config(model, weights)
    self.assertFalse(config["training"])
    self.assertTrue(config["all_parameters_frozen"])
    self.assertEqual(config["model_transform"]["min_size"], [800])
    self.assertEqual(config["model_transform"]["max_size"], 1333)
    self.assertEqual(config["model_transform"]["size_divisible"], 32)

if __name__ == "__main__": unittest.main()
