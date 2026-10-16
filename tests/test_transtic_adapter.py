"""CPU contract tests; no official checkpoint, training data, detector, or GPU."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from c3_neural_compression.evaluation import transtic_adapter as adapter

try:
  import torch
except ImportError:
  torch = None


class DimensionsTest(unittest.TestCase):
  def test_landscape_and_portrait(self):
    self.assertEqual(adapter.compute_dimensions(480, 640).as_dict(), {
        "original_hw": [480, 640], "detector_hw": [800, 1067],
        "codec_hw": [1024, 1280]})
    self.assertEqual(adapter.compute_dimensions(640, 480).detector_hw, (1067, 800))

  def test_maximum_edge_caps_without_cropping(self):
    dims = adapter.compute_dimensions(100, 1000)
    self.assertEqual(dims.detector_hw, (133, 1333))
    self.assertEqual(dims.codec_hw, (256, 1536))

  def test_square_and_already_aligned_long_edge(self):
    self.assertEqual(adapter.compute_dimensions(800, 800).codec_hw, (1024, 1024))
    self.assertEqual(adapter.compute_dimensions(800, 1024).codec_hw, (1024, 1024))

  def test_invalid_dimensions(self):
    for value in (0, -1, 3.2, True, "480"):
      with self.subTest(value=value), self.assertRaises(ValueError):
        adapter.compute_dimensions(value, 640)
    with self.assertRaises(ValueError):
      adapter.compute_dimensions(1, 100000)

  def test_official_release_urls_only_four_detection_points(self):
    for quality in range(1, 5):
      self.assertEqual(adapter.checkpoint_url(quality),
                       f"https://github.com/NYCU-MAPL/TransTIC/releases/download/v1.0/det_{quality}.pth.tar")
    for quality in (0, 5, True, "1", 1.0):
      with self.subTest(quality=quality), self.assertRaises(ValueError):
        adapter.checkpoint_url(quality)

  def test_prompt_configuration_is_fresh_and_matches_detection(self):
    config = adapter.prompt_config()
    self.assertEqual((config.ARCHITECT, config.MODEL_DECODER, config.NUM_TOKENS),
                     ("both", False, 16))
    self.assertEqual(config.DECODER_BLOCK, [1, 2, 3, 4])
    config.DECODER_BLOCK.append(5)
    self.assertEqual(adapter.prompt_config().DECODER_BLOCK, [1, 2, 3, 4])

  def test_import_requires_no_optional_dependencies(self):
    script = """
import builtins
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('torch', 'torchvision', 'detectron2', 'compressai', 'numpy', 'PIL'):
        raise AssertionError('Unexpected optional import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from c3_neural_compression.evaluation.transtic_adapter import compute_dimensions
assert compute_dimensions(480, 640).detector_hw == (800, 1067)
"""
    result = subprocess.run([sys.executable, "-c", script],
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, check=False)
    self.assertEqual(result.returncode, 0, result.stderr)

  def test_rate_record_is_json_safe_and_excludes_image(self):
    result = adapter.Reconstruction(object(), 10.5, 4.0, (480, 640),
                                    (800, 1067), (1024, 1280))
    value = json.loads(json.dumps(result.as_dict()))
    self.assertEqual(value["estimated_bits"], {"y": 10.5, "z": 4.0, "total": 14.5})
    self.assertNotIn("rgb", value)


class LoaderContractTest(unittest.TestCase):
  def test_dataparallel_prefix_is_removed_once(self):
    self.assertEqual(adapter._checkpoint_state_dict({"state_dict": {"module.a": 1}}), {"a": 1})
    self.assertEqual(adapter._checkpoint_state_dict({"state_dict": {"a": 1}}), {"a": 1})

  def test_malformed_checkpoint_fails_closed(self):
    for checkpoint in ({}, {"state_dict": {}}, {"state_dict": {"a": 1, "module.b": 2}},
                       {"state_dict": {2: 3}}, [1, 2]):
      with self.subTest(checkpoint=checkpoint), self.assertRaises(adapter.TransTICError):
        adapter._checkpoint_state_dict(checkpoint)

  def test_loading_is_strict_frozen_and_safe(self):
    model = mock.Mock(training=False)
    model.parameters.return_value = [SimpleNamespace(requires_grad=False)]
    model.to.return_value = model
    factory = mock.Mock(return_value=model)
    torch_mock = mock.Mock()
    torch_mock.load.return_value = {"state_dict": {"module.weight": 7}}
    with tempfile.TemporaryDirectory() as temporary:
      checkpoint = Path(temporary) / "det_1.pth.tar"
      checkpoint.write_bytes(b"dummy fixture")
      with mock.patch.object(adapter, "verify_upstream", return_value=Path(temporary)), \
           mock.patch.object(adapter, "_require_torch", return_value=torch_mock), \
           mock.patch.object(adapter, "_load_upstream_zoo", return_value=SimpleNamespace(
               image_models={adapter.MODEL_NAME: factory})):
        loaded = adapter.load_published_model(temporary, checkpoint, 1, "cpu")
    self.assertIs(loaded, model)
    self.assertEqual(factory.call_args.kwargs["quality"], 1)
    self.assertFalse(factory.call_args.kwargs["prompt_config"].MODEL_DECODER)
    torch_mock.load.assert_called_once_with(str(checkpoint), map_location="cpu", weights_only=True)
    model.load_state_dict.assert_called_once_with({"weight": 7}, strict=True)
    model.eval.assert_called_once_with()
    model.requires_grad_.assert_called_once_with(False)
    model.to.assert_called_once_with("cpu")

  def test_wrong_imported_compressai_is_rejected(self):
    with mock.patch.dict(sys.modules, {"compressai": SimpleNamespace(__file__="/different/compressai/__init__.py")}):
      with self.assertRaisesRegex(adapter.TransTICError, "Another compressai"):
        adapter._load_upstream_zoo(Path("/official/TransTIC"))

  def test_constructor_is_lazy_and_load_is_cached(self):
    instance = adapter.TransTICAdapter("/repo", "/checkpoint", 2, "cpu")
    with mock.patch.object(adapter, "load_published_model", return_value=object()) as load:
      self.assertIsNone(instance._model)
      self.assertIs(instance.load(), instance)
      instance.load()
      load.assert_called_once_with(Path("/repo"), Path("/checkpoint"), 2, "cpu")

  def test_missing_source_and_checkpoint_errors(self):
    with tempfile.TemporaryDirectory() as temporary:
      with self.assertRaisesRegex(adapter.TransTICError, "Missing TransTIC"):
        adapter.verify_upstream(temporary)
      with mock.patch.object(adapter, "verify_upstream", return_value=Path(temporary)):
        with self.assertRaisesRegex(adapter.TransTICError, "Missing local"):
          adapter.load_published_model(temporary, Path(temporary) / "absent", 1, "cpu")

  def test_requires_pinned_unmodified_source_but_allows_generated_version(self):
    with tempfile.TemporaryDirectory() as temporary:
      root = Path(temporary)
      for name in ("compressai/zoo/image.py", "compressai/models/tic_promptmodel.py",
                   "config/detection.yaml", "examples/detection.py",
                   "examples/utils/alignment.py", "LICENSE"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
      with mock.patch.object(adapter.subprocess, "check_output", side_effect=[
          adapter.UPSTREAM_COMMIT + "\n", "compressai/version.py\n"]):
        self.assertEqual(adapter.verify_upstream(root), root)
      for head, changed, message in (
          ("bad-commit", "", "HEAD"),
          (adapter.UPSTREAM_COMMIT, "compressai/models/tic_promptmodel.py\n", "modified")):
        with mock.patch.object(adapter.subprocess, "check_output", side_effect=[head, changed]):
          with self.assertRaisesRegex(adapter.TransTICError, message):
            adapter.verify_upstream(root)

  def test_failed_safe_checkpoint_load_never_retries_unsafe_pickle(self):
    torch_mock = mock.Mock()
    torch_mock.load.side_effect = RuntimeError("Unsupported pickle global")
    zoo = SimpleNamespace(image_models={adapter.MODEL_NAME: mock.Mock()})
    with tempfile.TemporaryDirectory() as temporary:
      checkpoint = Path(temporary) / "det_1.pth.tar"
      checkpoint.touch()
      with mock.patch.object(adapter, "verify_upstream", return_value=Path(temporary)), \
           mock.patch.object(adapter, "_require_torch", return_value=torch_mock), \
           mock.patch.object(adapter, "_load_upstream_zoo", return_value=zoo):
        with self.assertRaisesRegex(adapter.TransTICError, "Unsafe pickle loading was not attempted"):
          adapter.load_published_model(temporary, checkpoint, 1, "cpu")
    self.assertEqual(torch_mock.load.call_count, 1)
    self.assertTrue(torch_mock.load.call_args.kwargs["weights_only"])


@unittest.skipUnless(torch is not None, "Optional torch is not installed")
class TensorContractTest(unittest.TestCase):
  def setUp(self):
    self.original_hw = (100, 1000)
    self.dims = adapter.compute_dimensions(*self.original_hw)
    self.rgb = torch.full((3, *self.dims.detector_hw), 0.1234567)

  def fake_model(self, x_hat=None, likelihoods=None, training=False):
    class Model:
      def __init__(self):
        self.training = training
        self.seen = None
      def __call__(self, image):
        self.seen = image.detach().clone()
        return {"x_hat": image.clone() if x_hat is None else x_hat,
                "likelihoods": likelihoods if likelihoods is not None else {
                    "y": torch.tensor([0.5, 0.25]), "z": torch.tensor([0.125])}}
    return Model()

  def test_real_cpu_tensors_resize_up_and_back_and_count_bits(self):
    model = self.fake_model()
    result = adapter.reconstruct_detector_rgb(self.rgb, model, self.original_hw)
    self.assertEqual(tuple(model.seen.shape), (1, 3, 256, 1536))
    self.assertEqual(tuple(result.rgb.shape), (3, 133, 1333))
    self.assertEqual(result.estimated_bits, {"y": 3.0, "z": 3.0, "total": 6.0})
    self.assertTrue(torch.allclose(result.rgb, self.rgb, atol=1e-7))
    self.assertEqual(result.rgb.dtype, torch.float32)
    self.assertFalse(result.rgb.requires_grad)
    self.assertNotEqual(float(result.rgb[0, 0, 0]), round(0.1234567 * 255) / 255)

  def test_inverse_resize_before_clamping_matches_upstream(self):
    # Values outside [0,1] must be resampled before clamping.
    x_hat = torch.empty((1, 3, *self.dims.codec_hw))
    x_hat[..., ::2] = -1
    x_hat[..., 1::2] = 2
    expected = torch.nn.functional.interpolate(
        x_hat, size=self.dims.detector_hw, mode="bilinear", align_corners=False).clamp(0, 1)[0]
    result = adapter.reconstruct_detector_rgb(self.rgb, self.fake_model(x_hat=x_hat), self.original_hw)
    self.assertTrue(torch.equal(result.rgb, expected))

  def test_rejects_invalid_input_and_training_model(self):
    for value in (self.rgb.double(), self.rgb.unsqueeze(0), self.rgb + 2,
                  self.rgb * float("nan")):
      with self.assertRaises(ValueError):
        adapter.reconstruct_detector_rgb(value, self.fake_model(), self.original_hw)
    with self.assertRaisesRegex(adapter.TransTICError, "evaluation mode"):
      adapter.reconstruct_detector_rgb(self.rgb, self.fake_model(training=True), self.original_hw)

  def test_rejects_invalid_rates_and_reconstruction(self):
    for value in (0.0, -0.2, float("nan"), float("inf"), 1.1):
      with self.subTest(value=value), self.assertRaisesRegex(adapter.TransTICError, "likelihood"):
        adapter.reconstruct_detector_rgb(self.rgb, self.fake_model(likelihoods={
            "y": torch.tensor([value]), "z": torch.tensor([0.5])}), self.original_hw)
    for image in (torch.zeros((1, 3, 4, 4)), torch.full((1, 3, *self.dims.codec_hw), float("nan"))):
      with self.assertRaisesRegex(adapter.TransTICError, "x_hat"):
        adapter.reconstruct_detector_rgb(self.rgb, self.fake_model(x_hat=image), self.original_hw)

  def test_bgr_to_rgb_order_and_scale_no_detector_normalization(self):
    import numpy as np
    image = np.zeros((*self.original_hw, 3), dtype=np.uint8)
    mapped = np.empty((*self.dims.detector_hw, 3), dtype=np.uint8)
    mapped[..., 0], mapped[..., 1], mapped[..., 2] = 10, 20, 30
    instance = adapter.TransTICAdapter("/repo", "/checkpoint", 1, "cpu")
    model = self.fake_model()
    instance._model = model
    with mock.patch.object(adapter, "mapper_bgr", return_value=(mapped, self.dims)):
      result = instance.reconstruct_bgr(image)
    for channel, expected in enumerate((30 / 255, 20 / 255, 10 / 255)):
      self.assertAlmostEqual(float(result.rgb[channel, 0, 0]), expected, places=6)


if __name__ == "__main__":
  unittest.main()
