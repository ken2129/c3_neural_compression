"""Prepare C3 reconstructions and aggregate Phase 4 dataset-level rates."""

from __future__ import annotations

import argparse
import json
import os
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


def prepare_c3_outputs(c3_output_root: Path, annotation_file: Path,
                       subset_ids_file: Path, reconstruction_root: Path):
  """Validates per-datum artifacts, links PNGs by COCO name, aggregates rate."""
  subset_ids = coco_detection.load_subset_ids(subset_ids_file)
  annotation = _read_json(annotation_file)
  images = {item['id']: item for item in annotation.get('images', [])}
  if unknown := sorted(set(subset_ids) - set(images)):
    raise coco_detection.InputValidationError(
        f'Subset IDs absent from annotations: {unknown}'
    )
  reconstruction_root.mkdir(parents=True, exist_ok=True)
  rows = []
  for datum_index, image_id in enumerate(subset_ids):
    datum_dir = c3_output_root / f'datum_{datum_index:05d}'
    metrics = _read_json(datum_dir / 'metrics.json')
    metadata = metrics.get('datum_metadata', {})
    expected = images[image_id]
    if metrics.get('datum_index') != datum_index:
      raise coco_detection.InputValidationError(
          f'Datum index mismatch in {datum_dir}'
      )
    if metadata.get('image_id') != image_id:
      raise coco_detection.InputValidationError(
          f'Image ID mismatch in {datum_dir}: expected {image_id}, '
          f'got {metadata.get("image_id")}'
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
    target = reconstruction_root / expected['file_name']
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(reconstruction.resolve())
    os.replace(temporary, target)
    bits = metrics.get('estimated_bits', {})
    pixels = expected['width'] * expected['height']
    rows.append({
        'datum_index': datum_index,
        'image_id': image_id,
        'file_name': expected['file_name'],
        'pixels': pixels,
        'estimated_bits': {
            key: float(bits[key])
            for key in ('latents', 'synthesis_network', 'entropy_network', 'total')
        },
        'estimated_bpp': float(bits['total']) / pixels,
        'psnr': float(metrics['psnr_quantized']),
    })
  total_pixels = sum(row['pixels'] for row in rows)
  bit_keys = ('latents', 'synthesis_network', 'entropy_network', 'total')
  total_bits = {
      key: sum(row['estimated_bits'][key] for row in rows) for key in bit_keys
  }
  return {
      'rate_definition': 'entropy_estimated',
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
  parser.add_argument('--c3-output-root', type=Path, required=True)
  parser.add_argument('--annotation-file', type=Path, required=True)
  parser.add_argument('--subset-ids', type=Path, required=True)
  parser.add_argument('--reconstruction-root', type=Path, required=True)
  parser.add_argument('--summary-file', type=Path, required=True)
  return parser


def main(argv: Sequence[str] | None = None):
  args = _parser().parse_args(argv)
  summary = prepare_c3_outputs(
      args.c3_output_root, args.annotation_file, args.subset_ids,
      args.reconstruction_root
  )
  args.summary_file.parent.mkdir(parents=True, exist_ok=True)
  coco_detection.write_json(args.summary_file, summary)
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
