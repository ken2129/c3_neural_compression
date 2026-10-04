"""Collect verified common-pipeline runs; do not infer matched rates or BD-rate."""
import argparse
import math
from pathlib import Path
from c3_neural_compression.evaluation import comparison as common


def collect(run_dirs):
  rows, reference_manifest, reference_detector = [], None, None
  seen = set()
  for directory in map(Path, run_dirs):
    if directory.resolve() in seen:
      raise ValueError('Duplicate run directory')
    seen.add(directory.resolve())
    if common.read_json(directory / 'status.json').get('status') != 'complete':
      raise ValueError(f'Incomplete run: {directory}')
    manifest = common.read_json(directory / 'manifest.json')
    detector = common.read_json(directory / 'detector.json')
    if reference_manifest is None:
      reference_manifest, reference_detector = manifest, detector
    elif manifest != reference_manifest or detector != reference_detector:
      raise ValueError('Runs must use identical manifest and detector metadata')
    config = common.read_json(directory / 'config.json')
    summary = common.aggregate(manifest, common.read_json(directory / 'per_image.json'), config['condition'])
    saved = common.read_json(directory / 'rate_summary.json')
    if summary != saved:
      raise ValueError(f'Stale rate summary: {directory}')
    metrics = common.read_json(directory / 'metrics.json')
    if (metrics.get('image_ids') != manifest['image_ids']
        or metrics.get('official_cocoeval_executed') is not True
        or metrics.get('iou_type') != 'bbox'):
      raise ValueError('Metrics do not certify the complete manifest bbox evaluation')
    ap = metrics.get('box_ap')
    if isinstance(ap, bool) or not isinstance(ap, (int, float)) or not math.isfinite(ap) or not -1 <= ap <= 1:
      raise ValueError('Expected native COCO box_ap scale [-1,1]')
    rows.append({'run_dir': str(directory.resolve()), 'condition': config['condition'],
                 'estimated_bpp': summary['estimated_bpp'],
                 'image_count': summary['image_count'], 'metrics': metrics,
                 'config': config})
  if set(row['condition'] for row in rows) != set(common.COMPONENTS):
    raise ValueError('Need all three conditions: c3_image, c3_combined, transtic')
  return {'schema_version': 1, 'runs': rows,
          'interpretation': 'New implementation comparison, not seminar reproduction or isolated INR effect. Inspect measured rate overlap before comparing AP. No BD-rate claim.',
          'manifest': reference_manifest, 'detector': reference_detector}


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--run-dir', action='append', required=True)
  parser.add_argument('--output', required=True)
  args = parser.parse_args(argv)
  common.write_json(args.output, collect(args.run_dir))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
