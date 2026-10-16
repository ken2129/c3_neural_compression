"""CPU checks for subset integrity and fail-closed execution authorization."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent

def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class PreflightTests(unittest.TestCase):
    def test_dimensions_are_not_prior_image_ids(self):
        module=load('prepare_heldout')
        raw={'images':[{'image_id':7,'original_hw':[640,480]}], 'layers':[0,1,2,3]}
        self.assertEqual(module.explicit_image_ids(raw),{7})
        self.assertEqual(module.explicit_image_ids([1,9,11],root=True),{1,9,11})
        envelope={'content':[{'type':'text','text':json.dumps([2,5])}]}
        self.assertEqual(module.explicit_image_ids(envelope),{2,5})

    def test_selection_excludes_and_replays_without_AP(self):
        module=load('prepare_heldout')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ann=root/'annotation.json';hist=root/'history.json'
            ann.write_text(json.dumps({'images':[{'id':i,'file_name':f'{i:012d}.jpg','height':640,'width':480} for i in range(1,11)]}))
            hist.write_text(json.dumps({'image_ids':[3,7]}))
            first=module.prepare(ann,[hist],root/'a',n=4)
            second=module.prepare(ann,[hist],root/'b',n=4)
            self.assertEqual(first['image_ids'],second['image_ids'])
            self.assertFalse(set(first['image_ids'])&{3,7})
            self.assertEqual(first['eligible_count'],8)
            with self.assertRaises(FileExistsError):module.prepare(ann,[hist],root/'a',n=4)

    def test_runner_denies_missing_scope_before_GPU_imports(self):
        runner=load('storage_runner')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);approval=root/'approval.json';job=root/'job.json'
            approval.write_text(json.dumps({'approved':False}))
            job.write_text(json.dumps({'scope_approval_file':str(approval)}))
            with self.assertRaisesRegex(ValueError,'Missing approval'):
                runner.main(job)

    def test_supervisor_denies_different_plan_before_mutation(self):
        module=load('supervise')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);plan=root/'plan.json';approval=root/'approval.json';job=root/'job.json'
            plan.write_text('{}');approval.write_text(json.dumps({'approved':True,'plan_sha256':'wrong'}))
            job.write_text(json.dumps({'scope_approval_file':str(approval),'scope_plan_file':str(plan)}))
            with self.assertRaisesRegex(ValueError,'exact plan'):module.run(job)
            self.assertEqual(set(p.name for p in root.iterdir()),{'plan.json','approval.json','job.json'})


if __name__=='__main__':unittest.main()
