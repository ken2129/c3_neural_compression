"""Prepare C3 reconstructions and aggregate Phase 4 dataset-level rates."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Sequence

from PIL import Image

from c3_neural_compression.evaluation import coco_detection


def _read_json(path: Path):
  try:
    with path.open(encoding='utf-8') as stream:
      return json.load(stream)
  except (FileNotFoundError, json.JSONDecodeError) as error:
    raise coco_detection.InputValidationError(
        f'Cannot read JSON {path}: {error}'
    ) from error


def prepare_c3_outputs(c3_output_roots: Sequence[Path] | Path,
                       annotation_file: Path,
                       subset_ids_file: Path, image_path_map_file: Path):
  """Discovers C3 outputs by image ID, validates them, and aggregates rate."""
  subset_ids = coco_detection.load_subset_ids(subset_ids_file)
  if not subset_ids:
    raise coco_detection.InputValidationError('Subset manifest is empty')
  annotation = _read_json(annotation_file)
  images = {item['id']: item for item in annotation.get('images', [])}
  if unknown := sorted(set(subset_ids) - set(images)):
    raise coco_detection.InputValidationError(
        f'Subset IDs absent from annotations: {unknown}'
    )
  by_image_id = {}
  roots = ([c3_output_roots] if isinstance(c3_output_roots, Path)
           else list(c3_output_roots))
  if not roots:
    raise coco_detection.InputValidationError('No C3 output roots supplied')
  for root in roots:
    for metrics_path in sorted(root.glob('datum_*/metrics.json')):
      metrics = _read_json(metrics_path)
      metadata = metrics.get('datum_metadata', {})
      image_id = metadata.get('image_id')
      if not isinstance(image_id, int):
        raise coco_detection.InputValidationError(
            f'Missing integer image ID in {metrics_path}'
        )
      if image_id in by_image_id:
        raise coco_detection.InputValidationError(
            f'Duplicate C3 output for image ID {image_id}'
        )
      by_image_id[image_id] = (metrics_path.parent, metrics)
  if missing := sorted(set(subset_ids) - set(by_image_id)):
    raise coco_detection.InputValidationError(
        f'Missing C3 outputs for image IDs: {missing}'
    )
  rows = []
  path_rows = []
  expected_signature = None
  for image_id in subset_ids:
    datum_dir, metrics = by_image_id[image_id]
    metadata = metrics.get('datum_metadata', {})
    expected = images[image_id]
    signature = metrics.get('experiment_signature')
    if not isinstance(signature, dict) or not signature.get('sha256'):
      raise coco_detection.InputValidationError(
          f'Missing experiment signature for image ID {image_id}'
      )
    if expected_signature is None:
      expected_signature = signature
    elif signature != expected_signature:
      raise coco_detection.InputValidationError(
          f'Experiment signature mismatch for image ID {image_id}'
      )
    if metadata.get('file_name') != expected['file_name']:
      raise coco_detection.InputValidationError(
          f'File name mismatch for image ID {image_id}'
      )
    reconstruction = datum_dir / 'reconstruction.png'
    try:
      with Image.open(reconstruction) as image:
        size = image.size
        image.verify()
    except Exception as error:
      raise coco_detection.InputValidationError(
          f'Unreadable reconstruction for image ID {image_id}: {error}'
      ) from error
    expected_size = (expected['width'], expected['height'])
    if size != expected_size:
      raise coco_detection.InputValidationError(
          f'Size mismatch for image ID {image_id}: expected={expected_size}, '
          f'actual={size}'
      )
    path_rows.append({
        'image_id': image_id,
        'path': str(reconstruction.resolve()),
    })
    raw_bits = metrics.get('estimated_bits', {})
    bit_keys = ('latents', 'synthesis_network', 'entropy_network', 'total')
    try:
      bits = {key: float(raw_bits[key]) for key in bit_keys}
    except (KeyError, TypeError, ValueError) as error:
      raise coco_detection.InputValidationError(
          f'Invalid estimated_bits for image ID {image_id}: {raw_bits}'
      ) from error
    if not all(math.isfinite(value) for value in bits.values()):
      raise coco_detection.InputValidationError(
          f'Non-finite estimated_bits for image ID {image_id}: {bits}'
      )
    if not all(value >= 0 for value in bits.values()):
      raise coco_detection.InputValidationError(
          f'Negative estimated_bits for image ID {image_id}: {bits}'
      )
    component_total = (
        bits['latents'] + bits['synthesis_network'] + bits['entropy_network']
    )
    if not math.isclose(bits['total'], component_total,
                        rel_tol=1e-6, abs_tol=1e-3):
      raise coco_detection.InputValidationError(
          f'Rate breakdown mismatch for image ID {image_id}: total='
          f'{bits["total"]}, components={component_total}'
      )
    pixels = expected['width'] * expected['height']
    rows.append({
        'datum_index': metrics.get('datum_index'),
        'image_id': image_id,
        'file_name': expected['file_name'],
        'pixels': pixels,
        'estimated_bits': bits,
        'estimated_bpp': float(bits['total']) / pixels,
        'psnr': float(metrics['psnr_quantized']),
    })
  total_pixels = sum(row['pixels'] for row in rows)
  total_bits = {
      key: sum(row['estimated_bits'][key] for row in rows) for key in bit_keys
  }
  image_path_map_file.parent.mkdir(parents=True, exist_ok=True)
  coco_detection.write_json(image_path_map_file, {'images': path_rows})
  return {
      'rate_definition': 'entropy_estimated',
      'experiment_signature': expected_signature,
      'c3_output_roots': [str(root.resolve()) for root in roots],
      'dataset_bpp_definition': 'sum(estimated_bits) / sum(pixels)',
      'image_count': len(rows),
      'total_pixels': total_pixels,
      'estimated_bits': total_bits,
      'estimated_bpp': {
          key: value / total_pixels for key, value in total_bits.items()
      },
      'per_image_bpp_mean_auxiliary': (
          sum(row['estimated_bpp'] for row in rows) / len(rows)
      ),
      'images': rows,
  }


def _parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      '--c3-output-root', type=Path, action='append', required=True,
      help='Repeat for every chunk/retry output root to aggregate.',
  )
  parser.add_argument('--annotation-file', type=Path, required=True)
  parser.add_argument('--subset-ids', type=Path, required=True)
  parser.add_argument('--image-path-map', type=Path, required=True)
  parser.add_argument('--summary-file', type=Path, required=True)
  return parser


def main(argv: Sequence[str] | None = None):
  args = _parser().parse_args(argv)
  summary = prepare_c3_outputs(
      args.c3_output_root, args.annotation_file, args.subset_ids,
      args.image_path_map
  )
  args.summary_file.parent.mkdir(parents=True, exist_ok=True)
  coco_detection.write_json(args.summary_file, summary)
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
