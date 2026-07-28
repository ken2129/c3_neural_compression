"""Aggregate Phase 4 calibration points and render dependency-free SVG plots."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Sequence

from c3_neural_compression.evaluation import coco_detection


def _read_json(path: Path):
  try:
    return json.loads(path.read_text(encoding='utf-8'))
  except (FileNotFoundError, json.JSONDecodeError) as error:
    raise coco_detection.InputValidationError(
        f'Cannot read JSON {path}: {error}') from error


def subset_statistics(annotation_file: Path, subset_ids_file: Path):
  """Counts images, annotations, categories, and COCO area buckets."""
  annotation = _read_json(annotation_file)
  subset_ids = coco_detection.load_subset_ids(subset_ids_file)
  selected = set(subset_ids)
  known = {item['id'] for item in annotation.get('images', [])}
  if unknown := sorted(selected - known):
    raise coco_detection.InputValidationError(
        f'Subset IDs absent from annotations: {unknown}')
  counts = {'small': 0, 'medium': 0, 'large': 0}
  category_ids = set()
  annotation_count = 0
  for item in annotation.get('annotations', []):
    if item.get('image_id') not in selected:
      continue
    annotation_count += 1
    category_ids.add(item.get('category_id'))
    area = item.get('area')
    if not isinstance(area, (int, float)):
      bbox = item.get('bbox', ())
      if len(bbox) != 4:
        raise coco_detection.InputValidationError(
            f'Annotation {item.get("id")} has no valid area or bbox')
      area = bbox[2] * bbox[3]
    if area < 32 ** 2:
      counts['small'] += 1
    elif area < 96 ** 2:
      counts['medium'] += 1
    else:
      counts['large'] += 1
  return {
      'image_count': len(subset_ids),
      'annotation_count': annotation_count,
      'category_count': len(category_ids),
      'category_ids': sorted(category_ids),
      'object_area_counts': counts,
      'area_definition': {
          'small': 'area < 32^2',
          'medium': '32^2 <= area < 96^2',
          'large': 'area >= 96^2',
      },
  }


def _objective(fields):
  image_weight = float(fields['image_weight'])
  machine_weight = float(fields['machine_weight'])
  if image_weight > 0 and machine_weight > 0:
    return 'combined'
  if machine_weight > 0:
    return 'machine-only'
  return 'image-only'


def collect_conditions(condition_roots: Sequence[tuple[str, Path]]):
  """Loads validated Phase 4 summaries and COCO metrics."""
  if not condition_roots:
    raise coco_detection.InputValidationError('No calibration conditions')
  rows = []
  expected_image_ids = None
  labels = set()
  for label, root in condition_roots:
    if not label or label in labels:
      raise coco_detection.InputValidationError(
          f'Duplicate or empty condition label: {label!r}')
    labels.add(label)
    summary = _read_json(root / 'phase4_summary.json')
    detection = _read_json(root / 'coco_detection' / 'metrics.json')
    if summary.get('rate_definition') != 'entropy_estimated':
      raise coco_detection.InputValidationError(
          f'Unexpected rate definition for {label}')
    image_ids = [item['image_id'] for item in summary.get('images', [])]
    if expected_image_ids is None:
      expected_image_ids = image_ids
    elif image_ids != expected_image_ids:
      raise coco_detection.InputValidationError(
          f'Image ID sequence mismatch for {label}')
    fields = summary['experiment_signature']['fields']
    bpp = summary['estimated_bpp']
    psnr_values = [float(item['psnr']) for item in summary['images']]
    row = {
        'label': label,
        'objective': _objective(fields),
        'rd_weight': float(fields['rd_weight']),
        'image_weight': float(fields['image_weight']),
        'machine_weight': float(fields['machine_weight']),
        'image_count': int(summary['image_count']),
        'estimated_bpp_total': float(bpp['total']),
        'estimated_bpp_latents': float(bpp['latents']),
        'estimated_bpp_synthesis_network': float(bpp['synthesis_network']),
        'estimated_bpp_entropy_network': float(bpp['entropy_network']),
        'mean_psnr_db': sum(psnr_values) / len(psnr_values),
        'box_ap': float(detection['box_ap']),
        'box_ap50': float(detection['box_ap50']),
        'box_ap75': float(detection['box_ap75']),
        'experiment_signature': summary['experiment_signature']['sha256'],
        'code_commit': fields.get('code_commit'),
        'output_root': str(root.resolve()),
    }
    numeric = [value for key, value in row.items()
               if key.startswith(('estimated_', 'mean_', 'box_'))]
    if not all(math.isfinite(value) for value in numeric):
      raise coco_detection.InputValidationError(
          f'Non-finite calibration metric for {label}')
    rows.append(row)
  return sorted(rows, key=lambda row: (
      row['objective'], row['estimated_bpp_total']))


def _write_csv(path: Path, rows):
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open('w', encoding='utf-8', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)


def _write_svg(path: Path, rows, y_key: str, y_label: str):
  """Writes a compact SVG scatter/line plot grouped by objective."""
  width, height = 720, 480
  left, right, top, bottom = 80, 25, 35, 65
  plot_width, plot_height = width - left - right, height - top - bottom
  xs = [row['estimated_bpp_total'] for row in rows]
  ys = [row[y_key] for row in rows]
  x_min, x_max = min(xs), max(xs)
  y_min, y_max = min(ys), max(ys)
  x_pad = max((x_max - x_min) * 0.08, 1e-6)
  y_pad = max((y_max - y_min) * 0.12, 1e-6)
  x_min, x_max = x_min - x_pad, x_max + x_pad
  y_min, y_max = y_min - y_pad, y_max + y_pad

  def point(row):
    x = left + (row['estimated_bpp_total'] - x_min) / (x_max - x_min) * plot_width
    y = top + (y_max - row[y_key]) / (y_max - y_min) * plot_height
    return x, y

  colors = {'image-only': '#2563eb', 'combined': '#dc2626',
            'machine-only': '#7c3aed'}
  elements = [
      f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
      '<rect width="100%" height="100%" fill="white"/>',
      f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" '
      f'y2="{top + plot_height}" stroke="black"/>',
      f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" '
      'stroke="black"/>',
      f'<text x="{width / 2}" y="{height - 18}" text-anchor="middle" '
      'font-family="sans-serif">Entropy-estimated bpp</text>',
      f'<text x="18" y="{height / 2}" text-anchor="middle" '
      f'transform="rotate(-90 18 {height / 2})" font-family="sans-serif">'
      f'{y_label}</text>',
  ]
  groups = {}
  for row in rows:
    groups.setdefault(row['objective'], []).append(row)
  for group_index, (objective, group_rows) in enumerate(sorted(groups.items())):
    group_rows.sort(key=lambda row: row['estimated_bpp_total'])
    color = colors.get(objective, '#111827')
    points = ' '.join(f'{x:.2f},{y:.2f}' for x, y in map(point, group_rows))
    elements.append(
        f'<polyline points="{points}" fill="none" stroke="{color}" '
        'stroke-width="2"/>')
    for row in group_rows:
      x, y = point(row)
      elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{color}"/>')
    legend_y = top + group_index * 22
    elements.extend([
        f'<rect x="{width - 165}" y="{legend_y - 10}" width="12" height="12" '
        f'fill="{color}"/>',
        f'<text x="{width - 145}" y="{legend_y}" font-family="sans-serif" '
        f'font-size="13">{objective}</text>',
    ])
  for index in range(5):
    fraction = index / 4
    x_value = x_min + fraction * (x_max - x_min)
    x = left + fraction * plot_width
    y_value = y_max - fraction * (y_max - y_min)
    y = top + fraction * plot_height
    elements.extend([
        f'<text x="{x:.2f}" y="{top + plot_height + 22}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="12">{x_value:.3f}</text>',
        f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" '
        f'font-family="sans-serif" font-size="12">{y_value:.3f}</text>',
    ])
  elements.append('</svg>')
  path.write_text('\n'.join(elements), encoding='utf-8')


def write_outputs(output_dir: Path, rows, statistics=None,
                  original_metrics=None):
  """Writes JSON, CSV, and bpp-versus-quality SVG artifacts."""
  output_dir.mkdir(parents=True, exist_ok=True)
  result = {
      'rate_definition': 'entropy_estimated',
      'conditions': rows,
  }
  if statistics is not None:
    result['subset_statistics'] = statistics
  if original_metrics is not None:
    result['original_image_coco_metrics'] = original_metrics
  coco_detection.write_json(output_dir / 'calibration_summary.json', result)
  _write_csv(output_dir / 'calibration_summary.csv', rows)
  _write_svg(output_dir / 'bpp_map.svg', rows, 'box_ap', 'COCO box mAP')
  _write_svg(output_dir / 'bpp_psnr.svg', rows, 'mean_psnr_db', 'Mean PSNR (dB)')
  return result


def _parse_condition(value: str):
  try:
    label, root = value.split('=', maxsplit=1)
  except ValueError as error:
    raise argparse.ArgumentTypeError('Condition must be LABEL=ROOT') from error
  return label, Path(root)


def _parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--condition', type=_parse_condition, action='append',
                      required=True)
  parser.add_argument('--annotation-file', type=Path, required=True)
  parser.add_argument('--subset-ids', type=Path, required=True)
  parser.add_argument('--original-coco-metrics', type=Path)
  parser.add_argument('--output-dir', type=Path, required=True)
  return parser


def main(argv: Sequence[str] | None = None):
  args = _parser().parse_args(argv)
  rows = collect_conditions(args.condition)
  statistics = subset_statistics(args.annotation_file, args.subset_ids)
  original = (_read_json(args.original_coco_metrics)
              if args.original_coco_metrics else None)
  write_outputs(args.output_dir, rows, statistics, original)
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
