"""Opt-in issue41 protocol; preserve the original issue20 rate and inputs."""
from __future__ import annotations

import hashlib

from c3_neural_compression.evaluation import comparison as common
from c3_neural_compression.evaluation import shared_detector

ORIGINAL = 'original_rgb'
DETECTOR_INPUT = 'detector_input_v1'
PRIMARY_RATE = 'mean(total_estimated_bits / detector_resized_nonpadding_pixels)'


def validate_protocol(value):
  if value not in (ORIGINAL, DETECTOR_INPUT):
    raise ValueError(f'Unknown C3 input protocol: {value}')
  return value


def map_rgb_uint8(image):
  """Apply official D2 uint8 mapper before tensor conversion, without padding."""
  import numpy as np
  from detectron2.data.transforms import ResizeShortestEdge

  array = np.asarray(image)
  if array.ndim != 3 or array.shape[2] != 3 or array.dtype != np.uint8:
    raise ValueError('Mapper requires HWC uint8 RGB')
  resize = ResizeShortestEdge([800, 800], 1333)
  mapped = resize.get_transform(array).apply_image(array)
  expected = shared_detector.expected_resized_shape(*array.shape[:2])
  if mapped.shape[:2] != expected or mapped.dtype != np.uint8:
    raise ValueError('D2 mapper shape or dtype mismatch')
  return np.ascontiguousarray(mapped)


def mapped_metadata(mapped):
  return {
      'codec_input_protocol': DETECTOR_INPUT,
      'codec_input_height': int(mapped.shape[0]),
      'codec_input_width': int(mapped.shape[1]),
      'mapped_rgb_uint8_sha256': hashlib.sha256(mapped.tobytes()).hexdigest(),
  }


def load_reconstruction(path, original_hw):
  """Decode native rounded PNG at mapper size; never apply a second resize."""
  import numpy as np
  import torch
  from PIL import Image

  with Image.open(path) as image:
    if image.mode != 'RGB' or image.format != 'PNG':
      raise ValueError('Expected native RGB uint8 PNG reconstruction')
    expected = shared_detector.expected_resized_shape(*original_hw)
    if image.size != tuple(reversed(expected)):
      raise ValueError('Reconstruction must already have detector mapper size')
    array = np.array(image, dtype=np.uint8, copy=True)
  return torch.from_numpy(array).permute(2, 0, 1).float().div_(255.)


def rate_summary(manifest, rows, condition, input_protocol):
  """Validate all rows and bits first; opt into a separately identified axis."""
  validate_protocol(input_protocol)
  result = common.aggregate(manifest, rows, condition)
  if input_protocol == ORIGINAL:
    return result
  for row in result['images']:
    expected = shared_detector.expected_resized_shape(*row['original_hw'])
    if tuple(row['codec_hw']) != expected or tuple(row['detector_hw']) != expected:
      raise ValueError('C3 detector-input protocol dimensions mismatch')
    if row.get('codec_input_protocol') != DETECTOR_INPUT:
      raise ValueError('Missing detector-input provenance')
  result['original_pixel_pooled_bpp_auxiliary'] = result['estimated_bpp']
  result['estimated_bpp'] = result['native_detector_denominator_bpp_mean_auxiliary']
  result['dataset_bpp_definition'] = PRIMARY_RATE
  result['input_protocol'] = DETECTOR_INPUT
  result['total_detector_resized_pixels'] = sum(
      row['detector_hw'][0] * row['detector_hw'][1] for row in result['images'])
  result['output_precision'] = 'native rounded/clipped uint8 PNG'
  return result


def config_metadata(config):
  protocol = validate_protocol(config.get('input_protocol', ORIGINAL))
  return {
      'input_protocol': protocol,
      'primary_rate_definition': PRIMARY_RATE if protocol == DETECTOR_INPUT else
          'sum(estimated_bits) / sum(original_height * original_width)',
      'decode_resize_count': 0 if protocol == DETECTOR_INPUT else 1,
      'original_coordinates_retained': True,
      'actual_bitstream_generated': False,
  }


def collect_runs(run_dirs):
  """Collect only the new C3 protocol, retaining strict manifest/detector equality."""
  from pathlib import Path
  directories = [Path(path) for path in run_dirs]
  if not directories or len({p.resolve() for p in directories}) != len(directories):
    raise ValueError('Need unique nonempty run directories')
  runs = []
  reference = None
  for directory in directories:
    if common.read_json(directory / 'status.json').get('status') != 'complete':
      raise ValueError('Incomplete detector-input evaluation')
    config = common.read_json(directory / 'config.json')
    if (config.get('input_protocol') != DETECTOR_INPUT
        or config.get('condition') not in ('c3_image', 'c3_combined')):
      raise ValueError('Only detector-input C3 runs can be collected')
    manifest = common.read_json(directory / 'manifest.json')
    detector = common.read_json(directory / 'detector.json')
    identity = (manifest, detector)
    if reference is None:
      reference = identity
    elif identity != reference:
      raise ValueError('Exact manifest and detector metadata required')
    summary = rate_summary(manifest, common.read_json(directory / 'per_image.json'),
                           config['condition'], DETECTOR_INPUT)
    if summary != common.read_json(directory / 'rate_summary.json'):
      raise ValueError('Stale detector-input rate summary')
    metrics = common.read_json(directory / 'metrics.json')
    import math
    if (metrics.get('image_ids') != manifest['image_ids']
        or metrics.get('official_cocoeval_executed') is not True
        or metrics.get('iou_type') != 'bbox'):
      raise ValueError('Metrics do not certify all manifest IDs')
    for key in ('box_ap', 'box_ap50'):
      value = metrics.get(key)
      if type(value) not in (int,float) or not math.isfinite(value) or not -1 <= value <= 1:
        raise ValueError('Invalid COCO metric scale')
    runs.append({'run_dir':str(directory.resolve()),'condition':config['condition'],
                 'config':config,'rate_summary':summary,'metrics':metrics})
  if {run['condition'] for run in runs} != {'c3_image','c3_combined'}:
    raise ValueError('Both original and feature-loss C3 conditions required')
  return {'input_protocol':DETECTOR_INPUT,'runs':runs,
          'manifest':reference[0],'detector':reference[1],
          'interpretation':'Measured points only; inspect target proximity and between-method rate difference. No AP interpolation, paper reproduction or BD-rate claim.'}
