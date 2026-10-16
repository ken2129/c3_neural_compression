"""Synthetic fixtures check privacy, full rows and separate rate/AP reporting."""
import copy
import json
import unittest
from report import METRICS,public_report


def fixture():
    targets=[.1,.15,.225,.3];phases={}
    for name,ids in [('calibration',list(range(1,21))),('heldout',list(range(21,41)))]:
        rows=[]
        for method in ['c3_image','c3_combined']:
            for t in targets:
                rows.append({'method':method,'target_bpp':t,'status':'complete','image_count':20,
                             'official_pooled_COCO':True,'mean_estimated_bpp':t*(.94 if method=='c3_image' else 1.06),
                             'metrics':{k:.4 for k in METRICS},'private_path':'/private/secret','model_hash':'DO_NOT_PUBLISH'})
        phases[name]={'image_ids':ids,'points':rows,'baseline':{'status':'complete','image_count':20,'official_pooled_COCO':True,'metrics':{k:.5 for k in METRICS}}}
    return {'schema_version':1,'targets':targets,'phases':phases,'costs':{'gpu_seconds':3600,'wall_seconds':1800}}


class ReportTests(unittest.TestCase):
    def test_public_whitelist_drops_id_lists_paths_and_hashes(self):
        result=public_report(fixture());text=json.dumps(result)
        for forbidden in ['image_ids','private_path','/private','DO_NOT_PUBLISH','model_hash']:self.assertNotIn(forbidden,text)
        self.assertEqual(result['costs']['GPU_hours'],1)

    def test_target_tolerance_does_not_imply_pair_tolerance(self):
        result=public_report(fixture())['phases']['heldout']
        self.assertTrue(all(p['target_passed'] for p in result['points']))
        self.assertTrue(all(not p['rate_pair_passed'] for p in result['paired_comparisons']))

    def test_failed_and_undefined_points_are_not_removed(self):
        data=fixture();data['phases']['heldout']['points'][0]['status']='failed'
        data['phases']['heldout']['points'][1]['metrics']['box_ap_small']=-1
        result=public_report(data)['phases']['heldout']['points']
        self.assertEqual(len(result),8);self.assertIsNone(result[0]['mean_estimated_bpp'])
        self.assertIsNone(result[1]['box_ap_small_percent'])

    def test_missing_condition_and_overlap_fail(self):
        data=fixture();data['phases']['heldout']['points'].pop()
        with self.assertRaisesRegex(ValueError,'eight'):public_report(data)
        data=fixture();data['phases']['heldout']['image_ids'][0]=1
        with self.assertRaisesRegex(ValueError,'overlap'):public_report(data)

    def test_per_image_AP_or_partial_results_cannot_be_certified(self):
        data=fixture();data['phases']['heldout']['points'][0]['official_pooled_COCO']=False
        with self.assertRaisesRegex(ValueError,'official pooled'):public_report(data)
        data=fixture();data['phases']['heldout']['points'][0]['image_count']=19
        with self.assertRaisesRegex(ValueError,'full-set'):public_report(data)


if __name__=='__main__':unittest.main()
