"""Shared detector protocol tests; no Detectron2, weights, COCO data, or GPU."""

import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from c3_neural_compression.evaluation import shared_detector as detector


HAS_TORCH = importlib.util.find_spec('torch') is not None
HAS_COCO = importlib.util.find_spec('pycocotools') is not None


class FakeTensor:
  def __init__(self, values):
    self.values = values

  def detach(self):
    return self

  def cpu(self):
    return self

  def tolist(self):
    return self.values


def fake_instances(boxes=None, classes=None, scores=None):
  return types.SimpleNamespace(
      pred_boxes=types.SimpleNamespace(tensor=FakeTensor(
          [[10, 20, 30, 50]] if boxes is None else boxes)),
      pred_classes=FakeTensor([0] if classes is None else classes),
      scores=FakeTensor([0.001] if scores is None else scores))


class FakeImageList:
  def __init__(self, tensor, image_sizes):
    self.tensor = tensor
    self.image_sizes = image_sizes


class SharedDetectorContractTest(unittest.TestCase):

  def test_import_does_not_load_optional_dependencies(self):
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [sys.executable, '-c',
         'import sys; from c3_neural_compression.evaluation import shared_detector; '
         'assert not any(x in sys.modules for x in '
         '("torch", "torchvision", "detectron2", "numpy", "pycocotools"))'],
        env=environment, capture_output=True, text=True, check=False)
    self.assertEqual(result.returncode, 0, result.stderr)

  def test_checkpoint_hash_and_explicit_unverified_paper_identity(self):
    with tempfile.TemporaryDirectory() as directory:
      checkpoint = Path(directory) / 'local.pkl'
      checkpoint.write_bytes(b'local-test-checkpoint')
      expected = hashlib.sha256(b'local-test-checkpoint').hexdigest()
      result = detector.checkpoint_provenance(checkpoint, expected.upper())
      self.assertEqual(result['checkpoint_sha256'], expected)
      self.assertEqual(result['checkpoint_path'], str(checkpoint.resolve()))
      self.assertTrue(result['checkpoint_sha256_verified'])
      self.assertFalse(result['paper_checkpoint_identity_verified'])
      self.assertEqual(result['candidate_checkpoint_name'], 'model_final_280758.pkl')
      self.assertFalse(detector.checkpoint_provenance(checkpoint)[
          'checkpoint_sha256_verified'])
      with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
        detector.SharedCocoDetector(checkpoint, expected_sha256='0' * 64)
      with self.assertRaisesRegex(ValueError, 'hexadecimal'):
        detector.checkpoint_provenance(checkpoint, 'invalid')

  def test_missing_checkpoint_fails_before_heavy_imports(self):
    with tempfile.TemporaryDirectory() as directory:
      with self.assertRaisesRegex(FileNotFoundError, 'Local detector checkpoint'):
        detector.SharedCocoDetector(Path(directory) / 'missing.pkl')

  def test_package_fingerprint_tracks_relative_code_and_extension_bytes(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      package_a, package_b = root / 'a', root / 'b'
      for package in (package_a, package_b):
        package.mkdir()
        (package / '__init__.py').write_text('# same source\n')
        (package / 'model.py').write_text('VERSION = 1\n')
      extension = root / 'extension.so'
      extension.write_bytes(b'compiled-code')
      first = detector.package_fingerprint(package_a, extension)
      self.assertEqual(first, detector.package_fingerprint(package_b, extension))
      self.assertEqual(first['python_source_file_count'], 2)
      self.assertEqual(first['compiled_extension_sha256'],
                       hashlib.sha256(b'compiled-code').hexdigest())
      (package_a / 'model.py').write_text('VERSION = 2\n')
      self.assertNotEqual(first['python_source_sha256'],
                          detector.package_fingerprint(package_a, extension)[
                              'python_source_sha256'])
      extension.write_bytes(b'recompiled-code')
      self.assertNotEqual(first['compiled_extension_sha256'],
                          detector.package_fingerprint(package_b, extension)[
                              'compiled_extension_sha256'])

  def test_resize_shape_matches_short_edge_and_max_edge_rounding(self):
    for original, expected in [((480, 640), (800, 1067)),
                               ((640, 480), (1067, 800)),
                               ((1000, 2000), (667, 1333)),
                               ((2048, 500), (1333, 325)),
                               ((4, 4), (800, 800))]:
      with self.subTest(original=original):
        self.assertEqual(detector.expected_resized_shape(*original), expected)
    for invalid in (0, -1, 0.5, True):
      with self.assertRaises(ValueError):
        detector.expected_resized_shape(invalid, 400)

  def test_complete_sparse_coco_mapping_and_no_score_filter(self):
    self.assertEqual(len(detector.COCO_CATEGORY_IDS), 80)
    self.assertEqual(len(set(detector.COCO_CATEGORY_IDS)), 80)
    rows = detector.instances_to_coco(7, fake_instances(
        boxes=[[1, 2, 5, 8], [2, 3, 9, 10], [0, 0, 1, 1]],
        classes=[0, 11, 79], scores=[0.001, 0.01, 0.99]))
    self.assertEqual([row['category_id'] for row in rows], [1, 13, 90])
    self.assertEqual(rows[0], {'image_id': 7, 'category_id': 1,
                               'bbox': [1., 2., 4., 6.], 'score': 0.001})

  def test_rejects_invalid_instance_output(self):
    cases = [fake_instances(classes=[80]), fake_instances(classes=[-1]),
             fake_instances(classes=[1.5]), fake_instances(scores=[float('nan')]),
             fake_instances(boxes=[[1, 2, 0, 3]]),
             fake_instances(boxes=[[1, 2, float('inf'), 3]]),
             fake_instances(classes=[])]
    for value in cases:
      with self.subTest(value=value):
        with self.assertRaises(ValueError):
          detector.instances_to_coco(7, value)

  def test_empty_instances_are_valid(self):
    self.assertEqual(detector.instances_to_coco(
        7, fake_instances(boxes=[], classes=[], scores=[])), [])


class SharedCocoEvaluationTest(unittest.TestCase):

  def setUp(self):
    self.gt = mock.Mock()
    self.gt.getImgIds.return_value = [7, 9, 11]
    self.gt.getCatIds.return_value = [1, 13]
    self.gt.getAnnIds.return_value = [10, 20]
    self.gt.dataset = {'images': [{'id': 7}, {'id': 9}, {'id': 11}],
                       'categories': [{'id': 1}, {'id': 13}],
                       'annotations': [{'id': 10}]}
    self.empty = mock.Mock()
    self.coco_factory = mock.Mock(side_effect=[self.gt, self.empty])
    self.evaluator = mock.Mock()
    self.evaluator.stats = [0.1, 0.2, 0.3, 0.4, -1.0, 0.6]
    self.evaluation_factory = mock.Mock(return_value=self.evaluator)
    self.patcher = mock.patch.dict('sys.modules', {
        'pycocotools.coco': types.SimpleNamespace(COCO=self.coco_factory),
        'pycocotools.cocoeval': types.SimpleNamespace(COCOeval=self.evaluation_factory),
    })
    self.patcher.start()
    self.addCleanup(self.patcher.stop)

  def test_exact_ids_include_images_with_zero_predictions(self):
    predictions = detector.instances_to_coco(7, fake_instances())
    result = detector.evaluate_coco('annotations.json', predictions, [9, 7])
    self.assertEqual(self.evaluator.params.imgIds, [9, 7])
    self.assertEqual(result['image_ids'], [9, 7])
    self.assertEqual(result['prediction_count'], 1)
    self.assertEqual(result['image_count'], 2)
    self.assertEqual(result['ground_truth_annotation_count'], 2)
    self.assertEqual([result[key] for key in detector.METRICS],
                     self.evaluator.stats)
    self.assertTrue(result['official_cocoeval_executed'])
    self.assertFalse(result['additional_score_filtering'])
    self.gt.loadRes.assert_called_once_with(predictions)
    self.gt.getAnnIds.assert_called_once_with(imgIds=[9, 7])
    self.evaluator.evaluate.assert_called_once_with()
    self.evaluator.accumulate.assert_called_once_with()
    self.evaluator.summarize.assert_called_once_with()
    self.evaluation_factory.assert_called_once_with(
        self.gt, self.gt.loadRes.return_value, 'bbox')

  def test_empty_predictions_still_run_official_coco(self):
    self.evaluator.stats = [0.0, 0.0, 0.0, 0.0, -1.0, -1.0]
    result = detector.evaluate_coco('annotations.json', [], [7, 9])
    self.assertTrue(result['empty_predictions'])
    self.assertTrue(result['official_cocoeval_executed'])
    self.assertEqual(result['box_ap'], 0.0)
    self.assertEqual(result['box_ap_medium'], -1.0)
    self.assertEqual(self.empty.dataset['annotations'], [])
    self.assertEqual(self.empty.dataset['images'], self.gt.dataset['images'])
    self.empty.createIndex.assert_called_once_with()
    self.gt.loadRes.assert_not_called()
    self.evaluation_factory.assert_called_once_with(self.gt, self.empty, 'bbox')
    self.evaluator.evaluate.assert_called_once_with()

  def test_unknown_or_duplicate_or_empty_ids_fail(self):
    for ids, message in [([99], 'absent'), ([7, 7], 'duplicates'),
                         ([], 'empty'), ([7.2], 'integer'), ([True], 'integer')]:
      with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, message):
        detector.evaluate_coco('annotations.json', [], ids)
    self.evaluator.evaluate.assert_not_called()

  def test_out_of_subset_predictions_fail(self):
    with self.assertRaisesRegex(ValueError, 'outside the selected'):
      detector.evaluate_coco('annotations.json',
                             detector.instances_to_coco(9, fake_instances()), [7])

  def test_invalid_rows_fail_before_evaluation(self):
    base = detector.instances_to_coco(7, fake_instances())[0]
    for updates in [{'category_id': 12}, {'score': float('nan')},
                    {'score': 1.2}, {'bbox': [0, 0, -1, 2]},
                    {'bbox': [0, float('inf'), 1, 2]}]:
      with self.subTest(updates=updates):
        with self.assertRaises(ValueError):
          detector._validate_evaluation_rows([base | updates], {7}, {1, 13})


@unittest.skipUnless(HAS_COCO, 'Optional pycocotools is not installed')
class RealSyntheticCocoEvaluationTest(unittest.TestCase):

  def test_perfect_and_empty_predictions_use_official_ap(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'synthetic.json'
      path.write_text(json.dumps({
          'images': [{'id': 7, 'width': 64, 'height': 64},
                     {'id': 9, 'width': 64, 'height': 64}],
          'categories': [{'id': 1, 'name': 'person'}],
          'annotations': [{'id': 1, 'image_id': 7, 'category_id': 1,
                           'bbox': [10, 10, 20, 20], 'area': 400, 'iscrowd': 0}],
      }), encoding='utf-8')
      with contextlib.redirect_stdout(io.StringIO()):
        perfect = detector.evaluate_coco(path, [{
            'image_id': 7, 'category_id': 1,
            'bbox': [10, 10, 20, 20], 'score': 0.001}], [7, 9])
        empty = detector.evaluate_coco(path, [], [7, 9])
        no_gt = detector.evaluate_coco(path, [], [9])
      self.assertAlmostEqual(perfect['box_ap'], 1.0)
      self.assertEqual(perfect['image_ids'], [7, 9])
      self.assertEqual(empty['box_ap'], 0.0)
      self.assertEqual(empty['box_ap_medium'], -1.0)
      self.assertEqual(no_gt['box_ap'], -1.0)
      self.assertTrue(empty['official_cocoeval_executed'])


@unittest.skipUnless(HAS_TORCH, 'Optional PyTorch is not installed')
class SharedDetectorTensorTest(unittest.TestCase):

  def setUp(self):
    import torch
    self.torch = torch
    self.detector = detector.SharedCocoDetector.__new__(detector.SharedCocoDetector)
    self.detector.model = types.SimpleNamespace(
        device=torch.device('cpu'), training=False,
        pixel_mean=torch.tensor([103.530, 116.280, 123.675]).view(3, 1, 1),
        pixel_std=torch.ones(3, 1, 1))
    patcher = mock.patch.dict('sys.modules', {
        'detectron2.structures': types.SimpleNamespace(ImageList=FakeImageList)})
    patcher.start()
    self.addCleanup(patcher.stop)

  def _fake_constructor_modules(self, incompatible=None):
    model = self.detector.model
    model.backbone = types.SimpleNamespace(size_divisibility=32)
    model.proposal_generator = mock.Mock()
    model.eval = mock.Mock()
    model.requires_grad_ = mock.Mock()
    model.parameters = mock.Mock(return_value=[types.SimpleNamespace(requires_grad=False)])
    cfg = types.SimpleNamespace(
        MODEL=types.SimpleNamespace(
            WEIGHTS='', DEVICE='', PIXEL_MEAN=[103.530, 116.280, 123.675],
            PIXEL_STD=[1., 1., 1.], ROI_HEADS=types.SimpleNamespace(
                NUM_CLASSES=80, SCORE_THRESH_TEST=0.05, NMS_THRESH_TEST=0.5)),
        INPUT=types.SimpleNamespace(FORMAT='BGR', MIN_SIZE_TEST=800, MAX_SIZE_TEST=1333),
        TEST=types.SimpleNamespace(DETECTIONS_PER_IMAGE=100),
        merge_from_file=mock.Mock(), freeze=mock.Mock(),
        dump=mock.Mock(return_value='MODEL: test'))
    model_zoo = types.SimpleNamespace(get_config_file=mock.Mock(return_value='bundled.yaml'))
    loaded_paths = []

    class FakeCheckpointer:
      def __init__(self, given_model):
        self.model = given_model

      def load(self, path):
        loaded_paths.append(path)
        self._load_model({'model': {}})

      def _load_model(self, checkpoint):
        del checkpoint
        return incompatible or types.SimpleNamespace(
            missing_keys=[], unexpected_keys=[], incorrect_shapes=[])

    modules = {
        'detectron2': types.SimpleNamespace(
            __version__='test', model_zoo=model_zoo, __file__=__file__,
            _C=types.SimpleNamespace(__file__=__file__)),
        'detectron2.checkpoint': types.SimpleNamespace(DetectionCheckpointer=FakeCheckpointer),
        'detectron2.config': types.SimpleNamespace(get_cfg=lambda: cfg),
        'detectron2.modeling': types.SimpleNamespace(build_model=lambda _: model),
    }
    return modules, loaded_paths, cfg

  def test_constructor_loads_only_local_checkpoint_and_freezes_model(self):
    modules, loaded_paths, cfg = self._fake_constructor_modules()
    with tempfile.TemporaryDirectory() as directory:
      checkpoint = Path(directory) / 'weights.pkl'
      checkpoint.write_bytes(b'test')
      expected_sha = hashlib.sha256(b'test').hexdigest()
      with mock.patch.dict('sys.modules', modules):
        result = detector.SharedCocoDetector(checkpoint, device='cpu',
                                              expected_sha256=expected_sha)
    self.assertEqual(loaded_paths, [str(checkpoint.resolve())])
    self.detector.model.eval.assert_called_once_with()
    self.detector.model.requires_grad_.assert_called_once_with(False)
    cfg.freeze.assert_called_once_with()
    self.assertEqual(result.metadata()['score_thresh_test'], 0.05)
    self.assertEqual(result.metadata()['checkpoint_sha256'], expected_sha)
    self.assertFalse(result.metadata()['paper_checkpoint_identity_verified'])
    self.assertEqual(result.metadata()['preprocessing']['padding'], 'replicate_right_bottom')
    json.dumps(result.metadata())

  def test_partial_checkpoint_is_rejected_instead_of_random_parameters(self):
    modules, _, _ = self._fake_constructor_modules(types.SimpleNamespace(
        missing_keys=['roi_heads.box_predictor.cls_score.weight'],
        unexpected_keys=[], incorrect_shapes=[]))
    with tempfile.TemporaryDirectory() as directory:
      checkpoint = Path(directory) / 'partial.pkl'
      checkpoint.write_bytes(b'test')
      with mock.patch.dict('sys.modules', modules):
        with self.assertRaisesRegex(ValueError, 'checkpoint is incompatible'):
          detector.SharedCocoDetector(checkpoint, device='cpu')

  def test_bgr255_normalization_and_replicate_right_bottom(self):
    torch = self.torch
    rgb = torch.linspace(0, 1, 3 * 31 * 33).reshape(3, 31, 33)
    original = rgb.clone()
    images = self.detector._prepare_images(rgb)
    self.assertEqual(images.image_sizes, [(31, 33)])
    self.assertEqual(tuple(images.tensor.shape), (1, 3, 32, 64))
    expected = rgb.flip(0) * 255 - self.detector.model.pixel_mean
    torch.testing.assert_close(images.tensor[0, :, :31, :33], expected)
    torch.testing.assert_close(images.tensor[0, :, 31, :33], expected[:, -1, :])
    torch.testing.assert_close(images.tensor[0, :, :31, 33:],
                               expected[:, :, -1:].expand(3, 31, 31))
    torch.testing.assert_close(images.tensor[0, :, -1, -1], expected[:, -1, -1])
    torch.testing.assert_close(rgb, original)

  def test_already_divisible_and_hwc_array_match_without_quantization(self):
    import numpy as np
    torch = self.torch
    rgb = np.zeros((32, 64, 3), dtype=np.float32)
    rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2] = 0.123456, 0.00019, 1.0
    numpy_images = self.detector._prepare_images(rgb[:, ::-1, :])
    tensor_images = self.detector._prepare_images(torch.from_numpy(rgb).permute(2, 0, 1))
    self.assertEqual(tuple(numpy_images.tensor.shape), (1, 3, 32, 64))
    torch.testing.assert_close(numpy_images.tensor, tensor_images.tensor)
    expected_green = 0.00019 * 255.0 - 116.280
    self.assertAlmostEqual(float(numpy_images.tensor[0, 1, 0, 0]), expected_green, places=5)

  def test_tensor_input_validation(self):
    import numpy as np
    torch = self.torch
    invalid = [torch.zeros((3, 8, 8), dtype=torch.uint8),
               torch.zeros((8, 8, 3)), torch.zeros((1, 3, 8, 8)),
               torch.zeros((3, 0, 8)), torch.full((3, 8, 8), float('nan')),
               torch.full((3, 8, 8), 1.01), torch.full((3, 8, 8), -0.01),
               np.zeros((8, 8, 3), dtype=np.uint8),
               np.zeros((3, 8, 8), dtype=np.float32)]
    for value in invalid:
      with self.subTest(shape=value.shape):
        with self.assertRaises(ValueError):
          self.detector._prepare_images(value)

  def test_explicit_inference_uses_unpadded_geometry_and_original_scaling(self):
    torch = self.torch
    model = self.detector.model
    model.backbone = mock.Mock(return_value={'p2': 'features'})
    model.proposal_generator = mock.Mock(return_value=(['proposal'], {}))
    instances = fake_instances(boxes=[[80, 160, 400, 640]], classes=[11], scores=[0.07])
    instances.image_size = (800, 1067)
    model.roi_heads = mock.Mock(return_value=([instances], {}))

    def scale_to_original(value, height, width):
      self.assertFalse(torch.is_grad_enabled())
      self.assertIs(value, instances)
      self.assertEqual((height, width), (480, 640))
      x0, y0, x1, y1 = value.pred_boxes.tensor.tolist()[0]
      return fake_instances(boxes=[[x0 * width / 1067, y0 * height / 800,
                                    x1 * width / 1067, y1 * height / 800]],
                            classes=[11], scores=[0.07])

    postprocess = mock.Mock(side_effect=scale_to_original)
    with mock.patch.dict('sys.modules', {
        'detectron2.modeling.postprocessing': types.SimpleNamespace(
            detector_postprocess=postprocess)}):
      rows = self.detector.predict(torch.full((3, 800, 1067), 0.5),
                                   image_id=7, original_height=480, original_width=640)
    images, features, _ = model.proposal_generator.call_args.args
    self.assertEqual(images.image_sizes, [(800, 1067)])
    self.assertEqual(tuple(images.tensor.shape), (1, 3, 800, 1088))
    self.assertIs(model.backbone.call_args.args[0], images.tensor)
    model.roi_heads.assert_called_once_with(images, features, ['proposal'], None)
    postprocess.assert_called_once_with(instances, 480, 640)
    self.assertEqual(rows[0]['category_id'], 13)
    self.assertAlmostEqual(rows[0]['bbox'][0], 80 * 640 / 1067)
    self.assertAlmostEqual(rows[0]['bbox'][1], 96.0)
    self.assertAlmostEqual(rows[0]['bbox'][3], 288.0)

  def test_predict_rejects_unresized_or_double_resized_input(self):
    self.detector.model.backbone = mock.Mock()
    with mock.patch.dict('sys.modules', {
        'detectron2.modeling.postprocessing': types.SimpleNamespace(
            detector_postprocess=mock.Mock())}):
      with self.assertRaisesRegex(ValueError, 'already-resized'):
        self.detector.predict(self.torch.zeros((3, 480, 640)),
                              image_id=7, original_height=480, original_width=640)
    self.detector.model.backbone.assert_not_called()

  def test_original_rgb_resize_uses_d2_bgr_reader_and_transform(self):
    import numpy as np
    original = np.zeros((3, 5, 3), dtype=np.uint8)
    resized = np.array([[[10, 20, 30], [40, 50, 60]]], dtype=np.uint8)
    read_image = mock.Mock(return_value=original)
    augmentation = mock.Mock()
    augmentation.get_transform.return_value.apply_image.return_value = resized
    factory = mock.Mock(return_value=augmentation)
    with mock.patch.dict('sys.modules', {
        'detectron2.data': types.SimpleNamespace(
            transforms=types.SimpleNamespace(ResizeShortestEdge=factory)),
        'detectron2.data.detection_utils': types.SimpleNamespace(read_image=read_image),
    }):
      result = detector.resize_original_rgb('original.png')
    read_image.assert_called_once_with('original.png', format='BGR')
    factory.assert_called_once_with([800, 800], 1333)
    augmentation.get_transform.assert_called_once_with(original)
    self.assertEqual(tuple(result.shape), (3, 1, 2))
    self.torch.testing.assert_close(result[:, 0, 0], self.torch.tensor([30, 20, 10]) / 255)

  def test_metadata_is_a_defensive_copy(self):
    self.detector._metadata = {'checkpoint_sha256': 'f' * 64,
                               'preprocessing': {'padding': 'replicate_right_bottom'}}
    metadata = self.detector.metadata()
    metadata['preprocessing']['padding'] = 'zero'
    self.assertEqual(self.detector.metadata()['preprocessing']['padding'],
                     'replicate_right_bottom')
    json.dumps(self.detector.metadata())


if __name__ == '__main__':
  unittest.main()
