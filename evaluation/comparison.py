"""Strict, dependency-light manifests and rates for issue #20 comparisons.

Only entropy estimates are supported. A failed/missing/duplicate image prevents
an aggregate; predictions from successful images never define the evaluation set.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path

SCHEMA_VERSION = 1
COMPONENTS = {'c3_image': ('latents', 'synthesis_network', 'entropy_network'),
              'c3_combined': ('latents', 'synthesis_network', 'entropy_network'),
              'transtic': ('y', 'z')}


def read_json(path):
  return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
  temporary = path.with_suffix(path.suffix + '.tmp')
  temporary.write_text(text, encoding='utf-8')
  temporary.replace(path)


def sha256(path):
  result = hashlib.sha256()
  with Path(path).open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
      result.update(chunk)
  return result.hexdigest()


def positive_int(value, label):
  if type(value) is not int or value <= 0:
    raise ValueError(f'{label} must be a positive integer')
  return value


def image_ids(values):
  if not isinstance(values, list) or not values:
    raise ValueError('image_ids must be a nonempty list')
  for value in values:
    positive_int(value, 'image_id')
  if len(values) != len(set(values)):
    raise ValueError('Duplicate image IDs')
  return values


def dimensions(value, label):
  if not isinstance(value, (list, tuple)) or len(value) != 2:
    raise ValueError(f'{label} must be [height, width]')
  return tuple(positive_int(v, label) for v in value)


def validate_manifest(manifest):
  if manifest.get('schema_version') != SCHEMA_VERSION:
    raise ValueError('Unsupported manifest schema_version')
  ids = image_ids(manifest.get('image_ids'))
  rows = manifest.get('images')
  if not isinstance(rows, list) or len(rows) != len(ids):
    raise ValueError('Manifest images must match image_ids in order')
  paths = set()
  for image_id, row in zip(ids, rows):
    if type(row.get('image_id')) is not int or row['image_id'] != image_id:
      raise ValueError('Manifest images must match image_ids in order')
    dimensions(row.get('original_hw'), 'original_hw')
    path = str(Path(row['path']).resolve())
    if path in paths:
      raise ValueError('Duplicate image paths')
    paths.add(path)
  return manifest


def make_manifest(annotation_file, subset_file, image_root):
  from c3_neural_compression.evaluation.coco_detection import build_image_records
  ids = image_ids(read_json(subset_file))
  records, _ = build_image_records(Path(image_root), Path(annotation_file), ids)
  result = {'schema_version': SCHEMA_VERSION, 'image_ids': ids,
            'annotation_file': str(Path(annotation_file).resolve()),
            'annotation_sha256': sha256(annotation_file),
            'images': [{'image_id': r.image_id, 'file_name': r.file_name,
                        'original_hw': [r.height, r.width],
                        'path': str(r.path.resolve()), 'sha256': sha256(r.path)}
                       for r in records]}
  return validate_manifest(result)


def validate_assets(manifest):
  validate_manifest(manifest)
  if sha256(manifest['annotation_file']) != manifest['annotation_sha256']:
    raise ValueError('Annotation SHA256 mismatch')
  from PIL import Image
  for row in manifest['images']:
    if sha256(row['path']) != row['sha256']:
      raise ValueError(f"Image SHA256 mismatch: {row['image_id']}")
    with Image.open(row['path']) as image:
      if image.size != tuple(reversed(row['original_hw'])):
        raise ValueError(f"Image dimension mismatch: {row['image_id']}")
      image.verify()


def validate_bits(bits, condition):
  keys = COMPONENTS[condition]
  if not isinstance(bits, dict) or set(bits) != set(keys) | {'total'}:
    raise ValueError(f'Expected bit components {keys} plus total')
  for key, value in bits.items():
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
      raise ValueError(f'Invalid estimated bits {key}: {value}')
  if not math.isclose(bits['total'], math.fsum(bits[k] for k in keys), rel_tol=1e-6, abs_tol=1e-3):
    raise ValueError('Estimated bits total disagrees with components')
  return bits


def aggregate(manifest, rows, condition):
  validate_manifest(manifest)
  if condition not in COMPONENTS:
    raise ValueError(f'Unknown condition: {condition}')
  expected = {r['image_id']: r for r in manifest['images']}
  by_id = {}
  for row in rows:
    image_id = row.get('image_id')
    if type(image_id) is not int or image_id not in expected:
      raise ValueError(f'Unknown/invalid result image_id: {image_id}')
    if image_id in by_id:
      raise ValueError(f'Duplicate result image_id: {image_id}')
    by_id[image_id] = row
    if row.get('status') != 'ok':
      raise ValueError(f'Failed image prevents comparison: {image_id}')
    if row.get('rate_definition') != 'entropy_estimated':
      raise ValueError('Only entropy_estimated rates may be aggregated')
    if dimensions(row.get('original_hw'), 'original_hw') != tuple(expected[image_id]['original_hw']):
      raise ValueError(f'Original dimensions mismatch: {image_id}')
    dimensions(row.get('detector_hw'), 'detector_hw')
    dimensions(row.get('codec_hw'), 'codec_hw')
    validate_bits(row.get('estimated_bits'), condition)
  if set(by_id) != set(expected):
    raise ValueError(f'Missing images: {sorted(set(expected) - set(by_id))}')
  ordered = [by_id[i] for i in manifest['image_ids']]
  pixels = sum(r['original_hw'][0] * r['original_hw'][1] for r in ordered)
  totals = {k: math.fsum(r['estimated_bits'][k] for r in ordered)
            for k in (*COMPONENTS[condition], 'total')}
  return {'schema_version': SCHEMA_VERSION, 'condition': condition,
          'rate_definition': 'entropy_estimated',
          'dataset_bpp_definition': 'sum(estimated_bits) / sum(original_height * original_width)',
          'image_ids': manifest['image_ids'], 'image_count': len(ordered),
          'total_original_pixels': pixels, 'estimated_bits': totals,
          'estimated_bpp': totals['total'] / pixels,
          'per_image_bpp_mean_auxiliary': math.fsum(r['estimated_bits']['total'] / (r['original_hw'][0] * r['original_hw'][1]) for r in ordered) / len(ordered),
          'native_detector_denominator_bpp_mean_auxiliary': math.fsum(r['estimated_bits']['total'] / (r['detector_hw'][0] * r['detector_hw'][1]) for r in ordered) / len(ordered),
          'images': ordered}


def discover_c3(manifest, roots, condition, input_protocol='original_rgb'):
  """Require one run/signature and precisely the selected ID set; no best retry."""
  from c3_neural_compression.evaluation import detector_input_protocol as protocol
  protocol.validate_protocol(input_protocol)
  expected = set(manifest['image_ids'])
  from c3_neural_compression.evaluation.comparison_attempt import validate_completed_attempt
  found, signature = {}, None
  for root in roots:
    ledger = validate_completed_attempt(root)
    for output in ledger['outputs']:
      path = Path(root) / output['metrics_path']
      metrics = read_json(path)
      image_id = metrics.get('datum_metadata', {}).get('image_id')
      if type(image_id) is not int or image_id not in expected or image_id in found:
        raise ValueError(f'Unknown/duplicate C3 output ID: {image_id}')
      current = metrics.get('experiment_signature')
      if not isinstance(current, dict) or not current.get('sha256'):
        raise ValueError('Missing C3 experiment signature')
      if signature is not None and current != signature:
        raise ValueError('Mixed C3 experiment signatures')
      signature = current
      fields = current.get('fields', {})
      weight = fields.get('machine_weight')
      if weight is None or (condition == 'c3_image' and weight != 0) or (condition == 'c3_combined' and weight <= 0):
        raise ValueError('C3 objective does not match requested condition')
      if fields.get('codec_input_protocol', 'original_rgb') != input_protocol:
        raise ValueError('C3 input protocol does not match requested evaluation')
      validate_bits(metrics.get('estimated_bits'), condition)
      found[image_id] = {'metrics': metrics, 'path': path.parent / 'reconstruction.png'}
  if set(found) != expected:
    raise ValueError(f'Missing C3 outputs: {sorted(expected - set(found))}')
  from PIL import Image
  for row in manifest['images']:
    result = found[row['image_id']]
    source_hash = result['metrics']['datum_metadata'].get('original_file_sha256')
    if (not isinstance(source_hash, str) or len(source_hash) != 64
        or source_hash != row.get('sha256')):
      raise ValueError(f"C3 original file SHA256 missing or mismatched: {row['image_id']}")
    if result['metrics']['datum_metadata'].get('file_name') != row['file_name']:
      raise ValueError('C3 file_name mismatch')
    expected_hw = tuple(row['original_hw'])
    if input_protocol == protocol.DETECTOR_INPUT:
      expected_hw = protocol.shared_detector.expected_resized_shape(*row['original_hw'])
      metadata = result['metrics']['datum_metadata']
      if (metadata.get('codec_input_protocol') != input_protocol
          or (metadata.get('codec_input_height'), metadata.get('codec_input_width')) != expected_hw
          or result['metrics'].get('input_shape') != [*expected_hw, 3]):
        raise ValueError('C3 mapped input provenance/dimensions mismatch')
      from PIL import ImageOps
      with Image.open(row['path']) as original:
        mapped = protocol.map_rgb_uint8(ImageOps.exif_transpose(original).convert('RGB'))
      if metadata.get('mapped_rgb_uint8_sha256') != protocol.mapped_metadata(mapped)['mapped_rgb_uint8_sha256']:
        raise ValueError('C3 mapped input SHA256 mismatch')
    with Image.open(result['path']) as image:
      if image.size != tuple(reversed(expected_hw)):
        raise ValueError('C3 reconstruction dimensions mismatch')
      image.verify()
  return found, signature


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  sub = parser.add_subparsers(dest='command', required=True)
  make = sub.add_parser('manifest')
  make.add_argument('--annotation-file', required=True)
  make.add_argument('--subset-ids', required=True)
  make.add_argument('--image-root', required=True)
  make.add_argument('--output', required=True)
  check = sub.add_parser('check')
  check.add_argument('--manifest', required=True)
  args = parser.parse_args(argv)
  if args.command == 'manifest':
    write_json(args.output, make_manifest(args.annotation_file, args.subset_ids, args.image_root))
  else:
    validate_assets(read_json(args.manifest))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
