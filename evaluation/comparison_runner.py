"""Run one frozen issue-20 condition; refuse partial or mixed-rate results."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import time
from c3_neural_compression.evaluation import comparison as common


def environment():
  versions = {}
  for name in ('torch', 'torchvision', 'detectron2', 'compressai', 'numpy', 'Pillow', 'pycocotools', 'jax', 'jaxlib'):
    try:
      versions[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
      versions[name] = None
  root = Path(__file__).resolve().parents[1]
  def git(*args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
  return {'python': platform.python_version(), 'libraries': versions,
          'c3_commit': git('rev-parse', 'HEAD'),
          'c3_worktree_status': git('status', '--porcelain'),
          'source_sha256': {str(path.relative_to(root)): common.sha256(path)
                            for directory in ('evaluation', 'configs', 'experiments', 'machine_loss', 'model', 'utils')
                            for path in sorted((root / directory).rglob('*.py'))},
          'c3_tracked_diff_sha256': __import__('hashlib').sha256(git('diff', 'HEAD').encode()).hexdigest()}


def run_images(manifest, condition, reconstruct, detector, output_dir, evaluate,
               input_protocol='original_rgb'):
  """Dependency-injected inference loop; tests use mocks, never label them AP.

  reconstruct(image_row) returns (CHW float RGB, rate/dimensions dict).
  Every image attempt is durable. An exception aborts and forbids aggregation.
  """
  output_dir = Path(output_dir)
  rows, predictions = [], []
  for image in manifest['images']:
    started = time.perf_counter()
    try:
      rgb, row = reconstruct(image)
      row = dict(row, image_id=image['image_id'], status='ok',
                 rate_definition='entropy_estimated')
      common.validate_bits(row.get('estimated_bits'), condition)
      if tuple(row.get('original_hw', ())) != tuple(image['original_hw']):
        raise ValueError('Reconstruction original dimensions mismatch')
      result = detector.predict(rgb, image_id=image['image_id'],
                                original_height=image['original_hw'][0],
                                original_width=image['original_hw'][1])
      if any(p.get('image_id') != image['image_id'] for p in result):
        raise ValueError('Detector returned predictions for another image')
      row['evaluation_seconds'] = time.perf_counter() - started
      rows.append(row)
      predictions.extend(result)
      common.write_json(output_dir / 'per_image.json', rows)
      common.write_json(output_dir / 'predictions.json', predictions)
    except Exception as error:
      rows.append({'image_id': image['image_id'], 'status': 'failed',
                   'error': f'{type(error).__name__}: {error}',
                   'evaluation_seconds': time.perf_counter() - started})
      common.write_json(output_dir / 'per_image.json', rows)
      common.write_json(output_dir / 'status.json', {'status': 'failed', 'image_id': image['image_id']})
      raise
  from c3_neural_compression.evaluation import detector_input_protocol as protocol
  summary = protocol.rate_summary(manifest, rows, condition, input_protocol)
  # Pass ALL manifest IDs even when some images have no detections.
  metrics = evaluate(manifest['annotation_file'], predictions, manifest['image_ids'])
  common.write_json(output_dir / 'rate_summary.json', summary)
  common.write_json(output_dir / 'metrics.json', metrics)
  common.write_json(output_dir / 'status.json', {'status': 'complete'})
  return summary, metrics


def run(config, output_dir, validate_only=False):
  from c3_neural_compression.evaluation import shared_detector
  from c3_neural_compression.evaluation import detector_input_protocol as protocol
  input_protocol = protocol.validate_protocol(config.get('input_protocol', 'original_rgb'))
  manifest = common.read_json(config['manifest'])
  common.validate_assets(manifest)
  condition = config['condition']
  if condition not in common.COMPONENTS:
    raise ValueError('condition must be c3_image, c3_combined, or transtic')
  detector_config = config['detector']
  expected_hash = detector_config['sha256']
  if not isinstance(expected_hash, str) or len(expected_hash) != 64:
    raise ValueError('Fill in the exact common detector SHA256 before running')
  if common.sha256(detector_config['checkpoint']) != expected_hash:
    raise ValueError('Common detector SHA256 mismatch')
  c3, signature, codec = None, None, None
  if condition.startswith('c3_'):
    c3, signature = common.discover_c3(
        manifest, config['c3_output_roots'], condition, input_protocol=input_protocol)
  else:
    if input_protocol != protocol.ORIGINAL:
      raise ValueError('C3 input protocol cannot be selected for TransTIC')
    tt = config['transtic']
    if common.sha256(tt['checkpoint']) != tt['sha256']:
      raise ValueError('TransTIC checkpoint SHA256 mismatch')
  if validate_only:
    return {'status': 'assets_validated', 'image_count': len(manifest['image_ids']),
            'weights_loaded': False, 'detector_inference_run': False}
  output_dir = Path(output_dir)
  output_dir.mkdir(parents=True, exist_ok=True)
  if any(output_dir.iterdir()):
    raise ValueError('Use a new empty output directory for each run/attempt')
  common.write_json(output_dir / 'config.json', config)
  common.write_json(output_dir / 'manifest.json', manifest)
  if input_protocol != protocol.ORIGINAL:
    common.write_json(output_dir / 'protocol.json', protocol.config_metadata(config))
  common.write_json(output_dir / 'environment.json', environment())
  common.write_json(output_dir / 'status.json', {'status': 'initializing'})
  try:
    detector = shared_detector.SharedCocoDetector(detector_config['checkpoint'],
        device=config.get('device', 'cuda'), expected_sha256=expected_hash)
    common.write_json(output_dir / 'detector.json', detector.metadata())
    if condition == 'transtic':
      from c3_neural_compression.evaluation.transtic_adapter import TransTICAdapter
      codec = TransTICAdapter(tt['repository'], tt['checkpoint'], quality=tt['quality'], device=config.get('device', 'cuda'))
      common.write_json(output_dir / 'codec.json', codec.metadata())
      codec.load()
      def reconstruct(image):
        result = codec.reconstruct_image(image['path'])
        return result.rgb, result.as_dict()
    else:
      common.write_json(output_dir / 'c3_experiment_signature.json', signature)
      def reconstruct(image):
        item = c3[image['image_id']]
        if input_protocol == protocol.DETECTOR_INPUT:
          rgb = protocol.load_reconstruction(item['path'], image['original_hw'])
        else:
          rgb = shared_detector.resize_original_rgb(item['path'])
        row = {'original_hw': image['original_hw'],
                     'codec_hw': list(rgb.shape[-2:]) if input_protocol == protocol.DETECTOR_INPUT else image['original_hw'],
                     'detector_hw': list(rgb.shape[-2:]),
                     'estimated_bits': item['metrics']['estimated_bits'],
                     'reconstruction_sha256': common.sha256(item['path']),
                     'c3_timing_seconds': item['metrics'].get('timing_seconds'),
                     'reconstruction_precision': 'native C3 rounded/clipped uint8 PNG'}
        if input_protocol != protocol.ORIGINAL:
          row['codec_input_protocol'] = input_protocol
        return rgb, row
    return run_images(manifest, condition, reconstruct, detector, output_dir, shared_detector.evaluate_coco, input_protocol=input_protocol)
  except Exception as error:
    common.write_json(output_dir / 'status.json', {'status': 'failed', 'error': f'{type(error).__name__}: {error}'})
    raise


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--config', required=True)
  parser.add_argument('--output-dir', required=True)
  parser.add_argument('--validate-only', action='store_true')
  args = parser.parse_args(argv)
  result = run(common.read_json(args.config), args.output_dir, args.validate_only)
  if args.validate_only:
    print(json.dumps(result))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
