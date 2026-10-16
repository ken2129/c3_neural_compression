"""Evaluate saved external float and uint8 using the exact Issue49 detector."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

from codec import write
from preflight import sha


def run(root, method):
    root=Path(root).resolve();private=root/'var/issue50_evaluation_20261016'
    plan=json.loads((private/'preflight.json').read_text())
    sys.path.insert(0,str(Path(plan['runtime']).parent))
    import numpy as np
    import torch
    from PIL import Image
    from c3_neural_compression.evaluation import shared_detector
    torch.set_num_threads(2);torch.manual_seed(42)
    start=time.monotonic();state={'status':'running','pid':__import__('os').getpid(),'completed':0}
    status_path=private/(method+'_evaluation_status.json')
    try:
        ref=plan['detector']
        detector=shared_detector.SharedCocoDetector(ref['checkpoint_path'],device='cuda:0',expected_sha256=ref['checkpoint_sha256'])
        assert detector.metadata()==ref,'Shared detector differs from Issue49'
        manifest=json.loads(Path(plan['manifest']).read_text())
        for q in range(1,5):
            source=private/'results'/f'{method}_{q}'
            assert json.loads((source/'status.json').read_text())['status']=='complete'
            rows=json.loads((source/'per_image.json').read_text())
            assert [r['image_id'] for r in rows]==manifest['image_ids']
            for precision in ('float32','uint8'):
                out=private/'evaluations'/f'{method}_{q}_{precision}';out.mkdir(parents=True,exist_ok=False)
                write(out/'manifest.json',manifest);write(out/'detector.json',ref)
                write(out/'config.json',{'condition':method,'quality':q,'precision':precision,'plan_sha256':sha(private/'preflight.json')})
                write(out/'status.json',{'status':'running'})
                predictions=[]
                for im,row in zip(manifest['images'],rows):
                    assert time.monotonic()-start<2000,'Evaluation worker time budget'
                    assert shutil.disk_usage(private).free>plan['limits']['reserve_bytes']
                    assert sum(p.stat().st_size for p in private.rglob('*') if p.is_file())<plan['limits']['artifact_bytes']
                    path=Path(row['reconstruction_path'] if precision=='float32' else row['uint8_path'])
                    assert sha(path)==row['reconstruction_sha256' if precision=='float32' else 'uint8_sha256']
                    if precision=='float32':rgb=torch.from_numpy(np.load(path,allow_pickle=False))
                    else:
                        with Image.open(path) as image: arr=np.array(image,copy=True)
                        rgb=torch.from_numpy(arr).permute(2,0,1).float().div_(255)
                    assert tuple(rgb.shape[-2:])==tuple(row['detector_hw'])
                    predictions.extend(detector.predict(rgb,image_id=im['image_id'],original_height=im['original_hw'][0],original_width=im['original_hw'][1]))
                torch.cuda.synchronize()
                write(out/'predictions.json',predictions)
                metrics=shared_detector.evaluate_coco(manifest['annotation_file'],predictions,manifest['image_ids'])
                write(out/'metrics.json',metrics)
                write(out/'status.json',{'status':'complete'})
                state['completed']+=1;state['gpu_seconds']=time.monotonic()-start;write(status_path,state)
                print(method,q,precision,'complete',flush=True)
        state['status']='complete'
    except BaseException as error:
        state.update(status='failed',error_type=type(error).__name__,error=str(error));raise
    finally:
        state['gpu_seconds']=time.monotonic()-start;write(status_path,state)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--method',required=True);a=p.parse_args();run(a.root,a.method)
