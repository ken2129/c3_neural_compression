"""CPU-only rate reaggregation. Never equate a changed axis with changed inputs."""
from __future__ import annotations
import argparse
import csv
import math
from pathlib import Path
from c3_neural_compression.evaluation import comparison as common
from c3_neural_compression.evaluation import shared_detector


def inspect_run(path):
  path = Path(path)
  if common.read_json(path/'status.json').get('status') != 'complete':
    raise ValueError('Incomplete evaluation: ' + str(path))
  manifest = common.validate_manifest(common.read_json(path/'manifest.json'))
  config = common.read_json(path/'config.json')
  method = config['condition']
  if method not in (*common.COMPONENTS, 'adapt_icmh'):
    raise ValueError('Unknown codec method')
  bits_contract = 'transtic' if method == 'adapt_icmh' else method
  rows = common.read_json(path/'per_image.json')
  calculated = common.aggregate(manifest, rows, bits_contract)
  saved = common.read_json(path/'rate_summary.json')
  if method == 'adapt_icmh':
    calculated['condition'] = method
    calculated['quality'] = config['quality']
  if calculated != saved:
    raise ValueError('Saved rate summary differs from raw rows: ' + str(path))
  for row in rows:
    if tuple(row['detector_hw']) != shared_detector.expected_resized_shape(*row['original_hw']):
      raise ValueError('Detector resized dimensions do not match mapper')
  metrics = common.read_json(path/'metrics.json')
  if (metrics.get('official_cocoeval_executed') is not True
      or metrics.get('image_ids') != manifest['image_ids']
      or metrics.get('iou_type') != 'bbox'):
    raise ValueError('Metrics do not certify all manifest IDs')
  for key in ('box_ap', 'box_ap50'):
    if type(metrics.get(key)) not in (int,float) or not math.isfinite(metrics[key]) or not 0 <= metrics[key] <= 1:
      raise ValueError('Invalid AP scale')
  metadata = common.read_json(path/'detector.json')
  fingerprint = {p.name:common.sha256(p) for p in sorted(path.glob('*.json'))}
  fields = {
      'run_dir': str(path.resolve()), 'method': method,
      'image_count': len(manifest['image_ids']),
      'original_pixel_pooled_bpp': saved['estimated_bpp'],
      'original_pixel_per_image_mean_bpp': saved['per_image_bpp_mean_auxiliary'],
      'resized_nonpadding_per_image_mean_bpp': saved['native_detector_denominator_bpp_mean_auxiliary'],
      'box_AP_percent': metrics['box_ap']*100,
      'box_AP50_percent': metrics['box_ap50']*100,
      'codec_input_stage': 'original_image' if method.startswith('c3_') else 'detector_resized_image',
      'is_new_issue41_c3_measurement': False,
  }
  return {'point': fields, 'manifest': manifest, 'detector': metadata,
          'json_sha256': fingerprint, 'config': config}


def collect(paths):
  paths = list(paths)
  if len({Path(path).resolve() for path in paths}) != len(paths):
    raise ValueError('Duplicate run directory')
  runs = [inspect_run(path) for path in paths]
  if not runs:
    raise ValueError('Need completed runs')
  first = runs[0]
  for run in runs[1:]:
    if run['manifest'] != first['manifest'] or run['detector'] != first['detector']:
      raise ValueError('Exact manifest and full detector metadata required')
  return {'scope':'CPU_reaggregation_of_existing_results_no_new_inference',
          'exact_manifest_and_detector_metadata_equal':True,
          'interpretation':'C3 original-input points only change rate reporting here. They are not measurements of resize-before-compression protocol. External points retain official codec alignment and output precision. No paper AP reproduction claim.',
          'runs':runs}


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--run-dir',action='append',required=True)
  parser.add_argument('--output-dir',required=True)
  args = parser.parse_args(argv)
  output = Path(args.output_dir)
  output.mkdir(parents=True,exist_ok=False)
  result = collect(args.run_dir)
  common.write_json(output/'rate_audit.json',result)
  with (output/'rate_audit.csv').open('w',newline='') as stream:
    writer=csv.DictWriter(stream,fieldnames=list(result['runs'][0]['point']),lineterminator='\n')
    writer.writeheader();writer.writerows(run['point'] for run in result['runs'])
  print('Revalidated and reaggregated',len(result['runs']),'runs. No GPU work.')


if __name__ == '__main__':
  main()
