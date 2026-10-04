"""C3 identifies exact compressed source bytes, not just COCO IDs/dimensions."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from PIL import Image
from c3_neural_compression.evaluation import comparison as c
from c3_neural_compression.utils.data_loading import COCO2017Subset


class SourceHashTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    self.path = self.root / '7.png'
    Image.new('RGB',(4,3), color='red').save(self.path)
    c.write_json(self.root/'ann.json', {'images':[{'id':7,'file_name':'7.png','width':4,'height':3}]})
    c.write_json(self.root/'ids.json',[7])
    self.manifest = c.make_manifest(self.root/'ann.json',self.root/'ids.json',self.root)
    self.loader = COCO2017Subset(self.root,self.root/'ann.json',self.root/'ids.json')

  def test_hash_identifies_exact_decoded_bytes(self):
    original_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
    result = self.loader[0]
    self.assertEqual(result['original_file_sha256'], original_hash)
    self.assertEqual(result['array'].getpixel((0,0)),(255,0,0))
    Image.new('RGB',(4,3),color='blue').save(self.path)
    changed = self.loader[0]
    self.assertNotEqual(changed['original_file_sha256'], original_hash)
    self.assertEqual(changed['array'].getpixel((0,0)),(0,0,255))
    self.assertEqual(result['original_file_sha256'],original_hash)

  def test_changed_dimensions_after_loader_construction_rejected(self):
    Image.new('RGB',(5,3)).save(self.path)
    with self.assertRaisesRegex(ValueError,'changed dimensions'):
      self.loader[0]

  def test_file_replacement_after_read_does_not_split_hash_and_decode(self):
    encoded = self.path.read_bytes()
    def replace_then_return(_path):
      Image.new('RGB', (4,3), color='blue').save(self.path)
      return encoded
    with mock.patch.object(Path, 'read_bytes', replace_then_return):
      result = self.loader[0]
    self.assertEqual(result['original_file_sha256'], hashlib.sha256(encoded).hexdigest())
    self.assertEqual(result['array'].getpixel((0,0)), (255,0,0))
    self.assertNotEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), result['original_file_sha256'])

  def _write_attempt(self, source_hash=None):
    from c3_neural_compression.evaluation.comparison_attempt import ComparisonAttempt
    root = self.root / 'c3'
    attempt = ComparisonAttempt.start(root, [7])
    datum = root / 'datum_00000'
    datum.mkdir()
    Image.new('RGB', (4,3)).save(datum / 'reconstruction.png')
    metadata = {'image_id':7, 'file_name':'7.png'}
    if source_hash is not None:
      metadata['original_file_sha256'] = source_hash
    c.write_json(datum / 'metrics.json', {
        'attempt_id':attempt.attempt_id,'datum_index':0,
        'datum_metadata':metadata,
        'experiment_signature':{'sha256':'test', 'fields':{'machine_weight':0}},
        'estimated_bits':{'latents':4, 'synthesis_network':3, 'entropy_network':5, 'total':12}})
    c.write_json(datum / 'config.json', {'test_only':True})
    attempt.record_output(0,7)
    attempt.complete()
    return root

  def test_completed_c3_output_requires_exact_original_bytes(self):
    compressed_hash = self.loader[0]['original_file_sha256']
    root = self._write_attempt(compressed_hash)
    c.discover_c3(self.manifest, [root], 'c3_image')
    # Same image ID, filename and dimensions, but a newly frozen different image.
    Image.new('RGB', (4,3), color='blue').save(self.path)
    changed_manifest = c.make_manifest(self.root/'ann.json',self.root/'ids.json',self.root)
    with self.assertRaisesRegex(ValueError, 'original file SHA256'):
      c.discover_c3(changed_manifest, [root], 'c3_image')

  def test_legacy_output_without_original_hash_rejected(self):
    root = self._write_attempt()
    with self.assertRaisesRegex(ValueError, 'original file SHA256'):
      c.discover_c3(self.manifest, [root], 'c3_image')

  def test_completed_outputs_reject_modified_metrics(self):
    root = self._write_attempt(self.loader[0]['original_file_sha256'])
    path = root/'datum_00000/metrics.json'
    data = c.read_json(path)
    data['estimated_bits']['latents'] = 99
    c.write_json(path,data)
    with self.assertRaisesRegex(ValueError, 'hash|SHA256'):
      c.discover_c3(self.manifest, [root], 'c3_image')

  def test_discovery_rejects_running_failed_and_missing_attempt_ledgers(self):
    root = self._write_attempt(self.loader[0]['original_file_sha256'])
    path = root/'comparison_attempt.json'
    ledger = c.read_json(path)
    for status in ('running','failed'):
      with self.subTest(status=status):
        ledger['status'] = status
        c.write_json(path,ledger)
        with self.assertRaisesRegex(ValueError, 'completed'):
          c.discover_c3(self.manifest,[root],'c3_image')
    path.unlink()
    with self.assertRaisesRegex(ValueError, 'artifact'):
      c.discover_c3(self.manifest,[root],'c3_image')
