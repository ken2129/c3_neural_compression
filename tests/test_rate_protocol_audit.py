"""Negative CPU checks for existing-run audit and new-protocol collection."""
import copy
from pathlib import Path
import tempfile
import unittest
from c3_neural_compression.evaluation import comparison as c
from c3_neural_compression.evaluation import detector_input_protocol as p
from c3_neural_compression.evaluation import rate_protocol_audit as audit


class RateAuditTest(unittest.TestCase):
  def setUp(self):
    self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    self.manifest={'schema_version':1,'image_ids':[7],'images':[{'image_id':7,'original_hw':[41,67],'path':str(self.root/'input.png'),'file_name':'input.png'}]}

  def run_fixture(self,name,method,new=False):
    root=self.root/name;root.mkdir();hw=list(p.shared_detector.expected_resized_shape(41,67))
    bits={'y':8.,'z':2.,'total':10.} if method in ('transtic','adapt_icmh') else {'latents':8.,'synthesis_network':1.,'entropy_network':1.,'total':10.}
    row={'image_id':7,'status':'ok','rate_definition':'entropy_estimated','original_hw':[41,67],'detector_hw':hw,'codec_hw':hw if new or method in ('transtic','adapt_icmh') else [41,67],'estimated_bits':bits}
    cfg={'condition':method}
    if new:row['codec_input_protocol']=p.DETECTOR_INPUT;cfg['input_protocol']=p.DETECTOR_INPUT
    summary=p.rate_summary(self.manifest,[row],'transtic' if method=='adapt_icmh' else method,p.DETECTOR_INPUT if new else p.ORIGINAL)
    if method=='adapt_icmh':cfg['quality']=1;summary.update(condition=method,quality=1)
    docs={'manifest':self.manifest,'config':cfg,'per_image':[row],'rate_summary':summary,'detector':{'compiled_extension_hash':'same'},'metrics':{'image_ids':[7],'official_cocoeval_executed':True,'iou_type':'bbox','box_ap':.4,'box_ap50':.7},'status':{'status':'complete'}}
    for key,value in docs.items():c.write_json(root/(key+'.json'),value)
    return root

  def test_existing_adapt_and_c3_are_recomputed_with_exact_identity(self):
    paths=[self.run_fixture('a','adapt_icmh'),self.run_fixture('c','c3_image')]
    result=audit.collect(paths);self.assertTrue(result['exact_manifest_and_detector_metadata_equal'])
    self.assertEqual(result['runs'][0]['point']['codec_input_stage'],'detector_resized_image')
    self.assertEqual(result['runs'][1]['point']['codec_input_stage'],'original_image')
    self.assertFalse(result['runs'][1]['point']['is_new_issue41_c3_measurement'])
    with self.assertRaisesRegex(ValueError,'Duplicate'):audit.collect([paths[0],paths[0]])

  def test_stale_bits_failed_run_and_extension_mismatch_rejected(self):
    a=self.run_fixture('a','transtic');b=self.run_fixture('b','adapt_icmh')
    c.write_json(b/'detector.json',{'compiled_extension_hash':'other'})
    with self.assertRaisesRegex(ValueError,'Exact'):audit.collect([a,b])
    summary=c.read_json(a/'rate_summary.json');summary['estimated_bpp']+=.1;c.write_json(a/'rate_summary.json',summary)
    with self.assertRaisesRegex(ValueError,'Saved rate'):audit.inspect_run(a)
    c.write_json(a/'status.json',{'status':'failed'})
    with self.assertRaisesRegex(ValueError,'Incomplete'):audit.inspect_run(a)

  def test_new_c3_collector_rejects_protocol_mix_and_stale_summary(self):
    a=self.run_fixture('a','c3_image',True);b=self.run_fixture('b','c3_combined',True)
    self.assertEqual(len(p.collect_runs([a,b])['runs']),2)
    legacy=self.run_fixture('legacy','c3_image')
    with self.assertRaisesRegex(ValueError,'Only detector'):p.collect_runs([legacy,b])
    summary=c.read_json(b/'rate_summary.json');summary['estimated_bpp']+=.1;c.write_json(b/'rate_summary.json',summary)
    with self.assertRaisesRegex(ValueError,'Stale'):p.collect_runs([a,b])

  def test_new_collector_requires_both_methods_and_same_detector(self):
    a=self.run_fixture('a','c3_image',True);b=self.run_fixture('b','c3_combined',True)
    with self.assertRaisesRegex(ValueError,'Both'):p.collect_runs([a])
    c.write_json(b/'detector.json',{'compiled_extension_hash':'other'})
    with self.assertRaisesRegex(ValueError,'Exact'):p.collect_runs([a,b])


if __name__=='__main__':unittest.main()
