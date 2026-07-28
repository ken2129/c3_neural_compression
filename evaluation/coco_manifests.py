"""Generate and validate deterministic disjoint COCO subset manifests."""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import Sequence

from c3_neural_compression.evaluation import coco_detection


def validate_disjoint_manifests(paths: Sequence[Path], annotation_file: Path):
  """Returns manifest IDs after validating existence and pairwise disjointness."""
  annotation = coco_detection._read_json(  # pylint: disable=protected-access
      annotation_file
  )
  known_ids = {item['id'] for item in annotation.get('images', [])}
  result = {}
  owners = {}
  for path in paths:
    image_ids = coco_detection.load_subset_ids(path)
    if not image_ids:
      raise coco_detection.InputValidationError(f'Manifest is empty: {path}')
    if unknown := sorted(set(image_ids) - known_ids):
      raise coco_detection.InputValidationError(
          f'Manifest {path} has unknown image IDs: {unknown}'
      )
    for image_id in image_ids:
      if image_id in owners:
        raise coco_detection.InputValidationError(
            f'Image ID {image_id} overlaps manifests {owners[image_id]} and {path}'
        )
      owners[image_id] = path
    result[str(path)] = image_ids
  return result


def generate_manifests(annotation_file: Path, smoke_file: Path,
                       calibration_file: Path, main_file: Path, seed: int,
                       calibration_size: int, main_size: int):
  """Generates fixed calibration/main IDs excluding the smoke subset."""
  annotation = coco_detection._read_json(  # pylint: disable=protected-access
      annotation_file
  )
  all_ids = sorted(item['id'] for item in annotation.get('images', []))
  smoke_ids = coco_detection.load_subset_ids(smoke_file)
  smoke_id_set = set(smoke_ids)
  candidates = [image_id for image_id in all_ids if image_id not in smoke_id_set]
  random.Random(seed).shuffle(candidates)
  required = calibration_size + main_size
  if calibration_size < 1 or main_size < 1 or len(candidates) < required:
    raise coco_detection.InputValidationError(
        f'Invalid manifest sizes: calibration={calibration_size}, main={main_size}, '
        f'available={len(candidates)}'
    )
  calibration_ids = candidates[:calibration_size]
  main_ids = candidates[calibration_size:required]
  calibration_file.parent.mkdir(parents=True, exist_ok=True)
  main_file.parent.mkdir(parents=True, exist_ok=True)
  coco_detection.write_json(calibration_file, calibration_ids)
  coco_detection.write_json(main_file, main_ids)
  validate_disjoint_manifests(
      [smoke_file, calibration_file, main_file], annotation_file
  )
  return calibration_ids, main_ids


def _parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--annotation-file', type=Path, required=True)
  parser.add_argument('--smoke-file', type=Path, required=True)
  parser.add_argument('--calibration-file', type=Path, required=True)
  parser.add_argument('--main-file', type=Path, required=True)
  parser.add_argument('--seed', type=int, default=20260728)
  parser.add_argument('--calibration-size', type=int, default=20)
  parser.add_argument('--main-size', type=int, default=100)
  return parser


def main(argv=None):
  args = _parser().parse_args(argv)
  generate_manifests(
      args.annotation_file, args.smoke_file, args.calibration_file,
      args.main_file, args.seed, args.calibration_size, args.main_size
  )
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
