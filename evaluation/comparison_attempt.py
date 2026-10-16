"""Dependency-free, fail-closed provenance for a fresh C3 comparison attempt.

An attempt owns an initially empty output root for its entire lifetime. Failed
or interrupted attempts must use a new root; this policy is opt-in and does not
affect the ordinary image experiment's checkpoint/resume workflow.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid


SCHEMA_VERSION = 1
LEDGER_NAME = 'comparison_attempt.json'
_ARTIFACTS = {
    'metrics': 'metrics.json',
    'reconstruction': 'reconstruction.png',
    'config': 'config.json',
}


def _now():
  return datetime.now(timezone.utc).isoformat()


def _image_ids(values):
  if (not isinstance(values, list) or not values
      or any(type(value) is not int or value <= 0 for value in values)
      or len(values) != len(set(values))):
    raise ValueError('Comparison image_ids must be nonempty, unique positive ints')
  return list(values)


def _read_json(path):
  try:
    return json.loads(Path(path).read_text(encoding='utf-8'))
  except (OSError, ValueError) as error:
    raise ValueError(f'Cannot read comparison artifact: {path}') from error


def _sha256(path):
  digest = hashlib.sha256()
  with Path(path).open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
      digest.update(chunk)
  return digest.hexdigest()


def _canonical_json(value):
  return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _check_root(root):
  if root.is_symlink() or not root.is_dir():
    raise ValueError(f'Comparison output root must be a real directory: {root}')


def _validate_outputs(root, ledger):
  """Checks exact registered outputs, their identities, and their file hashes."""
  _check_root(root)
  if not isinstance(ledger, dict) or ledger.get('schema_version') != SCHEMA_VERSION:
    raise ValueError('Unsupported comparison attempt schema_version')
  attempt_id = ledger.get('attempt_id')
  if (not isinstance(attempt_id, str) or len(attempt_id) != 32
      or any(character not in '0123456789abcdef' for character in attempt_id)):
    raise ValueError('Invalid comparison attempt_id')
  image_ids = _image_ids(ledger.get('image_ids'))
  if 'config_snapshot' in ledger:
    snapshot = ledger['config_snapshot']
    if (not isinstance(snapshot, dict)
        or hashlib.sha256(_canonical_json(snapshot).encode('utf-8')).hexdigest()
        != ledger.get('config_sha256')
        or ledger.get('random_seed') != snapshot.get('random_seed')):
      raise ValueError('Comparison start-time config/seed provenance mismatch')
  outputs = ledger.get('outputs')
  if not isinstance(outputs, list) or len(outputs) != len(image_ids):
    raise ValueError('Comparison output list must exactly match all image_ids')
  expected_children = {LEDGER_NAME}
  for datum_index, (image_id, output) in enumerate(zip(image_ids, outputs)):
    if (not isinstance(output, dict)
        or type(output.get('image_id')) is not int
        or output['image_id'] != image_id
        or type(output.get('datum_index')) is not int
        or output['datum_index'] != datum_index):
      raise ValueError('Comparison output order/identity does not match image_ids')
    datum_name = f'datum_{datum_index:05d}'
    expected_children.add(datum_name)
    datum_root = root / datum_name
    if datum_root.is_symlink() or not datum_root.is_dir():
      raise ValueError(f'Missing/invalid comparison datum directory: {datum_name}')
    if {path.name for path in datum_root.iterdir()} != set(_ARTIFACTS.values()):
      raise ValueError(f'Unexpected/missing comparison datum artifacts: {datum_name}')
    for kind, filename in _ARTIFACTS.items():
      relative_path = f'{datum_name}/{filename}'
      if output.get(f'{kind}_path') != relative_path:
        raise ValueError(f'Invalid comparison artifact path: {kind}')
      path = root / relative_path
      if path.is_symlink() or not path.is_file():
        raise ValueError(f'Missing/invalid comparison artifact: {relative_path}')
      if _sha256(path) != output.get(f'{kind}_sha256'):
        raise ValueError(f'Comparison artifact SHA256 mismatch: {relative_path}')
    metrics = _read_json(datum_root / _ARTIFACTS['metrics'])
    if not isinstance(metrics, dict) or metrics.get('attempt_id') != attempt_id:
      raise ValueError('C3 metrics attempt_id does not match comparison attempt')
    metadata = metrics.get('datum_metadata')
    if (not isinstance(metadata, dict)
        or type(metadata.get('image_id')) is not int
        or metadata['image_id'] != image_id
        or type(metrics.get('datum_index')) is not int
        or metrics['datum_index'] != datum_index):
      raise ValueError('C3 metrics identity does not match comparison output list')
    if ('config_snapshot' in ledger
        and _read_json(datum_root / _ARTIFACTS['config']) != ledger['config_snapshot']):
      raise ValueError('C3 output config differs from comparison start-time config')
  # Checkpoints are not comparison results. All result files are enumerated
  # above, including config files; stray datum directories are never ignored.
  checkpoints = root / 'checkpoints'
  if checkpoints.exists() or checkpoints.is_symlink():
    if checkpoints.is_symlink() or not checkpoints.is_dir():
      raise ValueError('Invalid comparison checkpoint directory')
    expected_children.add('checkpoints')
  if {path.name for path in root.iterdir()} != expected_children:
    raise ValueError('Unexpected/missing files in comparison output root')
  return ledger


def validate_completed_attempt(output_root):
  """Returns a verified completed ledger; incomplete or changed roots fail."""
  root = Path(output_root)
  _check_root(root)
  ledger_path = root / LEDGER_NAME
  if ledger_path.is_symlink():
    raise ValueError('Comparison attempt ledger must not be a symlink')
  ledger = _read_json(ledger_path)
  if not isinstance(ledger, dict) or ledger.get('status') != 'completed':
    raise ValueError('C3 comparison attempt must have status completed')
  return _validate_outputs(root, ledger)


class ComparisonAttempt:
  """Exclusive writer for one fresh comparison root."""

  def __init__(self, root, ledger):
    self.root = root
    self.ledger = ledger

  @property
  def attempt_id(self):
    return self.ledger['attempt_id']

  @classmethod
  def start(cls, output_root, image_ids, config_snapshot=None):
    """Claims an empty directory before any image fitting or output writing."""
    if not output_root:
      raise ValueError('Comparison attempts require an output root')
    image_ids = _image_ids(image_ids)
    config_json = None
    if config_snapshot is not None:
      if not isinstance(config_snapshot, dict):
        raise ValueError('Comparison config_snapshot must be a dictionary')
      config_json = _canonical_json(config_snapshot)
    root = Path(output_root)
    if root.is_symlink():
      raise ValueError('Comparison output root must not be a symlink')
    root.mkdir(parents=True, exist_ok=True)
    _check_root(root)
    if any(root.iterdir()):
      raise ValueError(f'Comparison output root must be empty: {root}')
    ledger = {
        'schema_version': SCHEMA_VERSION,
        'attempt_id': uuid.uuid4().hex,
        'status': 'running',
        'image_ids': image_ids,
        'outputs': [],
        'started_at': _now(),
        'finished_at': None,
    }
    if config_json is not None:
      ledger['config_snapshot'] = json.loads(config_json)
      ledger['config_sha256'] = hashlib.sha256(config_json.encode('utf-8')).hexdigest()
      ledger['random_seed'] = ledger['config_snapshot'].get('random_seed')
    # O_EXCL is crucial: two contenders can both observe an empty directory.
    try:
      with (root / LEDGER_NAME).open('x', encoding='utf-8') as stream:
        json.dump(ledger, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    except FileExistsError as error:
      raise ValueError(f'Comparison output root is already claimed: {root}') from error
    attempt = cls(root, ledger)
    if {path.name for path in root.iterdir()} != {LEDGER_NAME}:
      error = ValueError('Comparison output root changed during preflight')
      attempt.fail(error)
      raise error
    return attempt

  @classmethod
  def from_config(cls, config):
    """Starts the explicitly enabled fixed-COCO attempt with its selected IDs."""
    if config.checkpointing.resume:
      raise ValueError('Comparison attempts cannot resume; choose a fresh root')
    dataset = config.dataset
    if dataset.name != 'coco2017':
      raise ValueError('Comparison guard requires the fixed COCO2017 dataset')
    image_ids = _image_ids(_read_json(dataset.subset_ids_file))
    skip = dataset.skip_examples
    count = dataset.num_examples
    if skip is not None:
      if type(skip) is not int or skip < 0:
        raise ValueError('Comparison skip_examples must be a nonnegative integer')
      image_ids = image_ids[skip:]
    if count is not None:
      if type(count) is not int or count <= 0:
        raise ValueError('Comparison num_examples must be a positive integer')
      image_ids = image_ids[:count]
    return cls.start(config.output_dir, image_ids, config_snapshot=config.to_dict())

  def _assert_running(self):
    saved = _read_json(self.root / LEDGER_NAME)
    if (self.ledger['status'] != 'running' or not isinstance(saved, dict)
        or saved.get('attempt_id') != self.attempt_id
        or saved.get('status') != 'running'):
      raise ValueError('Comparison attempt no longer owns a running ledger')

  def _write(self, ledger=None):
    ledger = self.ledger if ledger is None else ledger
    temporary = self.root / f'.{LEDGER_NAME}.{self.attempt_id}.tmp'
    try:
      with temporary.open('x', encoding='utf-8') as stream:
        json.dump(ledger, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
      os.replace(temporary, self.root / LEDGER_NAME)
      self.ledger = ledger
    finally:
      if temporary.exists():
        temporary.unlink()

  def record_output(self, datum_index, image_id):
    """Registers exactly one newly written datum from the current attempt."""
    self._assert_running()
    next_index = len(self.ledger['outputs'])
    if (type(datum_index) is not int or datum_index != next_index
        or next_index >= len(self.ledger['image_ids'])
        or type(image_id) is not int
        or image_id != self.ledger['image_ids'][next_index]):
      raise ValueError('Unexpected/duplicate comparison output identity or order')
    output = {'datum_index': datum_index, 'image_id': image_id}
    for kind, filename in _ARTIFACTS.items():
      relative_path = f'datum_{datum_index:05d}/{filename}'
      path = self.root / relative_path
      if path.is_symlink() or not path.is_file():
        raise ValueError(f'Missing/invalid comparison artifact: {relative_path}')
      output[f'{kind}_path'] = relative_path
      output[f'{kind}_sha256'] = _sha256(path)
    metrics = _read_json(self.root / output['metrics_path'])
    if not isinstance(metrics, dict) or metrics.get('attempt_id') != self.attempt_id:
      raise ValueError('C3 metrics attempt_id does not match comparison attempt')
    self.ledger['outputs'].append(output)
    self._write()

  def complete(self):
    """Commits success only after all exact artifacts have been revalidated."""
    self._assert_running()
    _validate_outputs(self.root, self.ledger)
    self._write(dict(self.ledger, status='completed', finished_at=_now()))

  def fail(self, error):
    """Keeps partial outputs for diagnosis but makes them ineligible to compare."""
    self._assert_running()
    self._write(dict(
        self.ledger, status='failed', finished_at=_now(),
        failure={'type': type(error).__name__, 'message': str(error)},
    ))
