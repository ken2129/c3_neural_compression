"""One frozen Detectron2 COCO bbox evaluator for C3 and TransTIC.

The codec boundary is an RGB float image in [0, 1], already resized with
Detectron2's ResizeShortestEdge(800, 1333). The detector does not resize,
quantize, or clamp it. Inference follows the reconstruction path in TransTIC
1132f62cbddc0122aa19f620453c3b0c4de49b2c, examples/detection.py and
examples/utils/predictor.py: normalized BGR255 features use replicate padding,
while proposals and output scaling retain the unpadded resized dimensions.

All optional numerical/detector dependencies are imported lazily. Supplying a
local checkpoint is mandatory; this module never downloads detector weights.
The model-zoo checkpoint name is only a candidate, not a verified paper ID.
"""

from __future__ import annotations

import copy
import hashlib
import math
import numbers
from pathlib import Path
from typing import Any, Mapping, Sequence


DETECTOR_CONFIG = 'COCO-Detection/faster_rcnn_R_50_FPN_3x.yaml'
CANDIDATE_CHECKPOINT = 'model_final_280758.pkl'
TRANSTIC_COMMIT = '1132f62cbddc0122aa19f620453c3b0c4de49b2c'
MIN_SIZE_TEST = 800
MAX_SIZE_TEST = 1333
PAD_DIVISOR = 32
METRICS = ('box_ap', 'box_ap50', 'box_ap75', 'box_ap_small',
           'box_ap_medium', 'box_ap_large')
# Detectron2's contiguous class order is COCO's sorted 80 thing-category IDs.
COCO_CATEGORY_IDS = (
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19,
    20, 21, 22, 23, 24, 25, 27, 28, 31, 32, 33, 34, 35, 36, 37, 38,
    39, 40, 41, 42, 43, 44, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55,
    56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 67, 70, 72, 73, 74, 75,
    76, 77, 78, 79, 80, 81, 82, 84, 85, 86, 87, 88, 89, 90,
)


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
  if isinstance(value, bool) or not isinstance(value, numbers.Integral):
    raise ValueError(f'{name} must be an integer')
  value = int(value)
  if positive and value <= 0:
    raise ValueError(f'{name} must be positive')
  return value


def _sha256_file(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
      digest.update(chunk)
  return digest.hexdigest()


def package_fingerprint(package_root: str | Path,
                        extension_path: str | Path) -> dict:
  """Fingerprints installed D2 code, without guessing its source Git commit.

  A version such as ``0.6`` spans many source commits. Relative source paths
  and their hashes avoid conflating distinct installs with that same version.
  Binary hashes deliberately distinguish extension builds as well.
  """
  root = Path(package_root).resolve()
  extension = Path(extension_path).resolve()
  sources = sorted(root.rglob('*.py'), key=lambda path: path.relative_to(root).as_posix())
  if not sources:
    raise ValueError(f'Cannot fingerprint Detectron2 Python sources: {root}')
  digest = hashlib.sha256()
  for path in sources:
    digest.update(path.relative_to(root).as_posix().encode('utf-8'))
    digest.update(b'\0')
    digest.update(_sha256_file(path).encode('ascii'))
    digest.update(b'\n')
  return {
      'python_source_sha256': digest.hexdigest(),
      'python_source_file_count': len(sources),
      'python_source_hash_format': 'sha256(sorted relative_posix_path + NUL + file_sha256_hex + LF)',
      'compiled_extension_sha256': _sha256_file(extension),
      'compiled_extension_size_bytes': extension.stat().st_size,
  }


def checkpoint_provenance(checkpoint_path: str | Path,
                          expected_sha256: str | None = None) -> dict:
  """Hashes an explicit local checkpoint before any model/dependency loading."""
  path = Path(checkpoint_path).expanduser().resolve()
  if not path.is_file():
    raise FileNotFoundError(f'Local detector checkpoint not found: {path}')
  if '?' in str(path) or '#' in str(path):
    raise ValueError('Detector checkpoint paths cannot contain URL query/fragment characters')
  if expected_sha256 is not None:
    if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
        or any(c not in '0123456789abcdefABCDEF' for c in expected_sha256)):
      raise ValueError('expected_sha256 must be a 64-character hexadecimal SHA256')
  actual = _sha256_file(path)
  if expected_sha256 is not None and actual != expected_sha256.lower():
    raise ValueError(f'Detector checkpoint SHA256 mismatch: expected '
                     f'{expected_sha256.lower()}, got {actual}')
  return {
      'checkpoint_path': str(path),
      'checkpoint_sha256': actual,
      'checkpoint_size_bytes': path.stat().st_size,
      'checkpoint_sha256_verified': expected_sha256 is not None,
      'candidate_checkpoint_name': CANDIDATE_CHECKPOINT,
      'paper_checkpoint_identity_verified': False,
  }


def expected_resized_shape(height: int, width: int) -> tuple[int, int]:
  """Returns Detectron2 ResizeShortestEdge's rounded (height, width)."""
  height = _integer(height, 'original_height', positive=True)
  width = _integer(width, 'original_width', positive=True)
  scale = MIN_SIZE_TEST / min(height, width)
  if height < width:
    new_height, new_width = MIN_SIZE_TEST, width * scale
  else:
    new_height, new_width = height * scale, MIN_SIZE_TEST
  if max(new_height, new_width) > MAX_SIZE_TEST:
    scale = MAX_SIZE_TEST / max(new_height, new_width)
    new_height, new_width = new_height * scale, new_width * scale
  return int(new_height + 0.5), int(new_width + 0.5)


def resize_original_rgb(path: str | Path, *, device: str = 'cpu'):
  """Reads/resizes like the official D2 loader; returns a CHW RGB float tensor.

  Use this once before a codec, or for an original/PNG baseline. Do not apply
  it to TransTIC's already detector-sized float reconstruction.
  """
  import torch
  from detectron2.data import transforms as transforms
  from detectron2.data.detection_utils import read_image

  image = read_image(str(path), format='BGR')
  resize = transforms.ResizeShortestEdge(
      [MIN_SIZE_TEST, MIN_SIZE_TEST], MAX_SIZE_TEST)
  resized = resize.get_transform(image).apply_image(image)
  return (torch.as_tensor(resized.astype('float32').transpose(2, 0, 1))
          .flip(0).div_(255.0).to(device))


def instances_to_coco(image_id: int, instances) -> list[dict]:
  """Converts ORIGINAL-size Instances to rows without additional thresholds."""
  image_id = _integer(image_id, 'image_id')
  boxes = instances.pred_boxes.tensor.detach().cpu().tolist()
  classes = instances.pred_classes.detach().cpu().tolist()
  scores = instances.scores.detach().cpu().tolist()
  if not len(boxes) == len(classes) == len(scores):
    raise ValueError(f'Malformed detector output for image ID {image_id}')
  rows = []
  for box, class_id, score in zip(boxes, classes, scores):
    class_id = _integer(class_id, 'predicted contiguous class ID')
    if not 0 <= class_id < len(COCO_CATEGORY_IDS):
      raise ValueError(f'Non-COCO contiguous class ID: {class_id}')
    if (len(box) != 4 or not all(math.isfinite(float(x)) for x in box)
        or not math.isfinite(float(score)) or not 0 <= float(score) <= 1):
      raise ValueError(f'Invalid detector box/score for image ID {image_id}')
    x0, y0, x1, y1 = map(float, box)
    if x1 < x0 or y1 < y0:
      raise ValueError(f'Inverted detector box for image ID {image_id}')
    rows.append({'image_id': image_id,
                 'category_id': COCO_CATEGORY_IDS[class_id],
                 'bbox': [x0, y0, x1 - x0, y1 - y0],
                 'score': float(score)})
  return rows


class SharedCocoDetector:
  """Frozen R50-FPN 3x with TransTIC-compatible reconstruction inference."""

  def __init__(self, checkpoint_path: str | Path, *, device: str = 'cuda',
               expected_sha256: str | None = None):
    provenance = checkpoint_provenance(checkpoint_path, expected_sha256)
    import detectron2
    import torch
    from detectron2 import _C, model_zoo
    from detectron2.checkpoint import DetectionCheckpointer
    from detectron2.config import get_cfg
    from detectron2.modeling import build_model

    class StrictDetectionCheckpointer(DetectionCheckpointer):
      """Keep D2 legacy conversions but refuse partially random detectors."""

      def _load_model(self, checkpoint):
        incompatible = super()._load_model(checkpoint)
        problems = {
            name: list(getattr(incompatible, name, ()))
            for name in ('missing_keys', 'unexpected_keys', 'incorrect_shapes')
            if getattr(incompatible, name, ())
        }
        if problems:
          raise ValueError(f'Detector checkpoint is incompatible with '
                           f'{DETECTOR_CONFIG}: {problems}')
        return incompatible

    cfg = get_cfg()
    cfg.merge_from_file(model_zoo.get_config_file(DETECTOR_CONFIG))
    cfg.MODEL.WEIGHTS = provenance['checkpoint_path']
    cfg.MODEL.DEVICE = device
    cfg.freeze()
    self.cfg = cfg
    self.model = build_model(cfg)
    StrictDetectionCheckpointer(self.model).load(cfg.MODEL.WEIGHTS)
    self.model.eval()
    self.model.requires_grad_(False)
    if (self.model.training or
        any(parameter.requires_grad for parameter in self.model.parameters())):
      raise RuntimeError('Shared detector must be frozen and in eval mode')
    if (cfg.INPUT.FORMAT != 'BGR' or cfg.INPUT.MIN_SIZE_TEST != MIN_SIZE_TEST
        or cfg.INPUT.MAX_SIZE_TEST != MAX_SIZE_TEST
        or cfg.MODEL.ROI_HEADS.NUM_CLASSES != len(COCO_CATEGORY_IDS)
        or self.model.backbone.size_divisibility != PAD_DIVISOR
        or self.model.proposal_generator is None):
      raise ValueError('Detector configuration does not match the shared COCO protocol')
    if (list(cfg.MODEL.PIXEL_MEAN) != [103.530, 116.280, 123.675]
        or list(cfg.MODEL.PIXEL_STD) != [1.0, 1.0, 1.0]):
      raise ValueError('Detector normalization does not match pinned TransTIC')
    self._metadata = {
        **provenance,
        'name': 'detectron2_faster_rcnn_R_50_FPN_3x',
        'checkpoint_parameter_compatibility': 'strict_after_detectron2_legacy_conversion',
        'config_name': DETECTOR_CONFIG,
        'config_yaml': cfg.dump(),
        'device': str(self.model.device),
        'torch_version': torch.__version__,
        'detectron2_version': getattr(detectron2, '__version__', 'unknown'),
        'detectron2_installed_code': package_fingerprint(
            Path(detectron2.__file__).resolve().parent, _C.__file__),
        'transtic_reference_commit': TRANSTIC_COMMIT,
        'input': {'layout': 'CHW torch or HWC numpy', 'color': 'RGB',
                  'dtype': 'float32', 'range': [0.0, 1.0],
                  'already_resized': True, 'short_edge': MIN_SIZE_TEST,
                  'max_edge': MAX_SIZE_TEST, 'detector_resizes': False},
        'preprocessing': {'color': 'BGR', 'scale': 255.0,
                          'pixel_mean': list(cfg.MODEL.PIXEL_MEAN),
                          'pixel_std': list(cfg.MODEL.PIXEL_STD),
                          'padding': 'replicate_right_bottom',
                          'padding_divisor': PAD_DIVISOR,
                          'proposal_image_size': 'unpadded_resized',
                          'output_image_size': 'original'},
        'score_thresh_test': float(cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST),
        'nms_thresh_test': float(cfg.MODEL.ROI_HEADS.NMS_THRESH_TEST),
        'detections_per_image': int(cfg.TEST.DETECTIONS_PER_IMAGE),
        'additional_score_filtering': False,
        'contiguous_to_coco_category_ids': list(COCO_CATEGORY_IDS),
        'training': False,
        'all_parameters_frozen': True,
    }

  def metadata(self) -> dict:
    """Returns a detached, JSON-serializable provenance/configuration record."""
    return copy.deepcopy(self._metadata)

  def _prepare_images(self, image_rgb):
    import torch
    import torch.nn.functional as functional
    from detectron2.structures import ImageList

    if isinstance(image_rgb, torch.Tensor):
      tensor = image_rgb.detach()
      if tensor.ndim != 3 or tensor.shape[0] != 3:
        raise ValueError('Tensor input must be a CHW RGB image with three channels')
    else:
      import numpy as np
      image = np.asarray(image_rgb)
      if image.ndim != 3 or image.shape[-1] != 3:
        raise ValueError('Array input must be an HWC RGB image with three channels')
      if not np.issubdtype(image.dtype, np.floating):
        raise ValueError('RGB input must be floating point in [0, 1], not uint8')
      tensor = torch.as_tensor(np.array(image, copy=True)).permute(2, 0, 1)
    if not torch.is_floating_point(tensor):
      raise ValueError('RGB input must be floating point in [0, 1], not uint8')
    height, width = map(int, tensor.shape[-2:])
    if height <= 0 or width <= 0:
      raise ValueError('RGB image dimensions must be positive')
    tensor = tensor.to(device=self.model.device, dtype=torch.float32)
    if not bool(torch.isfinite(tensor).all()):
      raise ValueError('RGB input contains non-finite values')
    if float(tensor.min()) < 0.0 or float(tensor.max()) > 1.0:
      raise ValueError('RGB input must be in [0, 1]; clamp codec output before evaluation')
    normalized = ((tensor.flip(0) * 255.0 - self.model.pixel_mean)
                  / self.model.pixel_std)
    padded = functional.pad(normalized.unsqueeze(0),
                            (0, (-width) % PAD_DIVISOR,
                             0, (-height) % PAD_DIVISOR), mode='replicate')
    # Construct directly: ImageList.from_tensors uses zero padding, which is
    # not the TransTIC reconstruction feature path.
    return ImageList(padded, [(height, width)])

  def predict(self, image_rgb, *, image_id: int, original_height: int,
              original_width: int) -> list[dict]:
    """Runs one already-resized image and returns original-space COCO boxes."""
    import torch
    from detectron2.modeling.postprocessing import detector_postprocess

    image_id = _integer(image_id, 'image_id')
    original_height = _integer(original_height, 'original_height', positive=True)
    original_width = _integer(original_width, 'original_width', positive=True)
    if self.model.training:
      raise RuntimeError('Shared detector must remain in eval mode')
    with torch.inference_mode():
      images = self._prepare_images(image_rgb)
      expected = expected_resized_shape(original_height, original_width)
      if images.image_sizes != [expected]:
        raise ValueError(f'Expected already-resized RGB dimensions {expected}, '
                         f'got {images.image_sizes}; do not resize twice')
      features = self.model.backbone(images.tensor)
      proposals, _ = self.model.proposal_generator(images, features, None)
      instances, _ = self.model.roi_heads(images, features, proposals, None)
      if len(instances) != 1:
        raise RuntimeError('Shared detector output batch length must be one')
      result = detector_postprocess(instances[0], original_height, original_width)
      return instances_to_coco(image_id, result)


def _validate_evaluation_rows(predictions: Sequence[Mapping[str, Any]],
                              selected: set[int], categories: set[int]) -> list[dict]:
  rows = []
  for row in predictions:
    if not isinstance(row, Mapping):
      raise ValueError('Every COCO prediction must be a mapping')
    image_id = _integer(row.get('image_id'), 'prediction image_id')
    category_id = _integer(row.get('category_id'), 'prediction category_id')
    if image_id not in selected:
      raise ValueError(f'Prediction image ID {image_id} is outside the selected IDs')
    if category_id not in categories or category_id not in COCO_CATEGORY_IDS:
      raise ValueError(f'Prediction category ID {category_id} is not a known COCO category')
    box = row.get('bbox')
    if (not isinstance(box, (list, tuple)) or len(box) != 4 or
        not all(isinstance(x, numbers.Real) and math.isfinite(x) for x in box)
        or box[2] < 0 or box[3] < 0):
      raise ValueError(f'Invalid COCO xywh bbox for image ID {image_id}')
    score = row.get('score')
    if (not isinstance(score, numbers.Real) or not math.isfinite(score)
        or not 0 <= score <= 1):
      raise ValueError(f'Invalid COCO score for image ID {image_id}')
    rows.append({'image_id': image_id, 'category_id': category_id,
                 'bbox': list(map(float, box)), 'score': float(score)})
  return rows


def evaluate_coco(annotation_file: str | Path,
                  predictions: Sequence[Mapping[str, Any]],
                  image_ids: Sequence[int]) -> dict:
  """Runs official pycocotools bbox AP on exactly the supplied image IDs.

  An image with no detections is still evaluated. Empty prediction sets also
  run COCOeval, preserving its -1 convention for undefined area/category AP.
  AP values use the native [0, 1] COCO scale, not percentages.
  """
  selected = [_integer(value, 'image_id') for value in image_ids]
  if not selected:
    raise ValueError('Evaluation image IDs must not be empty')
  if len(selected) != len(set(selected)):
    raise ValueError('Evaluation image IDs must not contain duplicates')
  from pycocotools.coco import COCO
  from pycocotools.cocoeval import COCOeval

  ground_truth = COCO(str(annotation_file))
  if missing := sorted(set(selected) - set(ground_truth.getImgIds())):
    raise ValueError(f'Image IDs absent from COCO annotations: {missing}')
  rows = _validate_evaluation_rows(
      predictions, set(selected), set(ground_truth.getCatIds()))
  if rows:
    # Older COCO annotation exports may omit info. Current loadRes reads it.
    ground_truth.dataset.setdefault('info', {})
    detections = ground_truth.loadRes(rows)
  else:
    # COCO.loadRes([]) indexes the first annotation and fails in several
    # supported releases. Build a genuine empty COCO result instead.
    detections = COCO()
    detections.dataset = {
        'images': copy.deepcopy(ground_truth.dataset['images']),
        'categories': copy.deepcopy(ground_truth.dataset['categories']),
        'annotations': [],
    }
    detections.createIndex()
  evaluator = COCOeval(ground_truth, detections, 'bbox')
  evaluator.params.imgIds = list(selected)
  evaluator.evaluate()
  evaluator.accumulate()
  evaluator.summarize()
  return {
      **{name: float(evaluator.stats[index])
         for index, name in enumerate(METRICS)},
      'image_ids': selected,
      'image_count': len(selected),
      'prediction_count': len(rows),
      'ground_truth_annotation_count': len(ground_truth.getAnnIds(imgIds=selected)),
      'empty_predictions': not rows,
      'official_cocoeval_executed': True,
      'iou_type': 'bbox',
      'metric_scale': '0_to_1; -1 means undefined',
      'additional_score_filtering': False,
  }
