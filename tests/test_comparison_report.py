"""Collector uses rechecked rows and identical experiment/evaluation assets."""
import copy
from pathlib import Path
import tempfile
import unittest
from c3_neural_compression.evaluation import comparison as c
from c3_neural_compression.evaluation.comparison_report import collect


class ReportTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    self.manifest = {'schema_version':1, 'image_ids':[7], 'images':[{'image_id':7,'path':'/test/7.png','original_hw':[3,4]}]}
    self.runs = []
    for condition, components in c.COMPONENTS.items():
      directory = self.root / condition
      self.runs.append(directory)
      bits = {key:4.0 for key in components}
      bits['total'] = sum(bits.values())
      row = {'image_id':7,'status':'ok','rate_definition':'entropy_estimated','original_hw':[3,4],'detector_hw':[800,1067],'codec_hw':[1024,1280],'estimated_bits':bits}
      for name,value in {'status':{'status':'complete'},'manifest':self.manifest,'detector':{'sha256':'fixed'},'config':{'condition':condition},'per_image':[row],'rate_summary':c.aggregate(self.manifest,[row],condition),'metrics':{'test_only':True,'image_ids':[7],'official_cocoeval_executed':True,'iou_type':'bbox','box_ap':0.0}}.items():
        c.write_json(directory / f'{name}.json',value)

  def test_complete_collection(self):
    self.assertEqual(len(collect(self.runs)['runs']),3)

  def test_rejects_partial_duplicate_or_incomplete(self):
    for runs in (self.runs[:2],self.runs+self.runs[:1]):
      with self.subTest(runs=runs), self.assertRaises(ValueError):
        collect(runs)
    c.write_json(self.runs[0]/'status.json',{'status':'failed'})
    with self.assertRaisesRegex(ValueError,'Incomplete'):
      collect(self.runs)

  def test_rejects_detector_mismatch(self):
    c.write_json(self.runs[0]/'detector.json',{'sha256':'other'})
    with self.assertRaisesRegex(ValueError,'identical'):
      collect(self.runs)

  def test_rejects_stale_summary(self):
    p=self.runs[0]/'rate_summary.json'
    summary=c.read_json(p)
    summary['estimated_bpp']=123
    c.write_json(p,summary)
    with self.assertRaisesRegex(ValueError,'Stale'):
      collect(self.runs)
