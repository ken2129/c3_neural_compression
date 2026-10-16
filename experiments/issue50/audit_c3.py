"""Recompute all reused C3/JPEG pooled AP, preserving the existing artifacts."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
from codec import write
from preflight import sha


def run(root):
    root=Path(root);private=root/'var/issue50_evaluation_20261016'
    plan=json.loads((private/'preflight.json').read_text());sys.path.insert(0,str(Path(plan['runtime']).parent))
    from c3_neural_compression.evaluation import shared_detector
    manifest=json.loads(Path(plan['manifest']).read_text())
    runs={k:v['evaluation'] for k,v in plan['c3_runs'].items()};runs['baseline']=plan['baseline']
    audits={}
    for name,path in runs.items():
        path=Path(path);predictions=json.loads((path/'predictions.json').read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            metrics=shared_detector.evaluate_coco(manifest['annotation_file'],predictions,manifest['image_ids'])
        assert metrics==json.loads((path/'metrics.json').read_text())
        audits[name]={'official_AP_recomputed':True,'prediction_sha256':sha(path/'predictions.json'),'metrics_sha256':sha(path/'metrics.json')}
    write(private/'c3_baseline_metric_audit.json',{'status':'passed','runs':audits})
    print('Nine reused C3/JPEG pooled AP values independently recomputed exactly')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);run(p.parse_args().root)
