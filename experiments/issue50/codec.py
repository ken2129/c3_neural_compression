"""One bounded external codec worker; retains float and rounded PNG artifacts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import traceback


def write(path, value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
    temporary.replace(path)


def run(root, method):
    root=Path(root).resolve();private=root/'var/issue50_evaluation_20261016'
    plan=json.loads((private/'preflight.json').read_text())
    sys.path.insert(0,str(Path(plan['runtime']).parent))
    from preflight import sha
    import numpy as np
    import torch
    from PIL import Image
    from c3_neural_compression.evaluation import comparison as common, transtic_adapter, shared_detector
    from adapt_icmh_adapter import AdaptICMHAdapter
    assert torch.cuda.is_available() and torch.cuda.device_count()==1
    torch.set_num_threads(2);torch.manual_seed(42)
    assert sha(plan['manifest'])==plan['manifest_sha256']
    manifest=json.loads(Path(plan['manifest']).read_text());common.validate_assets(manifest)
    started=time.monotonic();state={'status':'running','pid':os.getpid(),'method':method,'started_unix':time.time(),'codec_attempts':0,'completed':0,'gpu_seconds':0}
    status_path=private/(method+'_codec_status.json')
    def guard(next_bytes=0):
        state['gpu_seconds']=time.monotonic()-started
        write(status_path,state)
        sizes=sum(p.stat().st_size for p in private.rglob('*') if p.is_file())
        assert sizes+next_bytes < plan['limits']['artifact_bytes'], 'Artifact budget'
        assert shutil.disk_usage(private).free-next_bytes > plan['limits']['reserve_bytes'], 'Disk reserve'
        assert state['gpu_seconds'] < 3000, 'Worker time guard (combined supervisor enforces total)'
    try:
        guard()
        for asset in (a for a in plan['assets'] if a['method']==method):
            q=asset['quality'];out=private/'results'/f'{method}_{q}'
            out.mkdir(parents=True,exist_ok=False)
            a=asset['asset'];assert sha(a['checkpoint'])==a['sha256']
            write(out/'config.json',asset);write(out/'manifest.json',manifest)
            write(out/'status.json',{'status':'running'})
            if method=='adapt_icmh':
                codec=AdaptICMHAdapter(a['repository'],a['checkpoint'],a['sha256'],q)
            else:
                from transtic_official import OfficialTransTIC
                decision=json.loads((private/'alignment_decision.json').read_text())
                assert decision['selected']=='official_resize'
                codec=OfficialTransTIC(a['repository'],a['checkpoint'],a['sha256'],q)
            write(out/'codec.json',codec.metadata())
            rows=[]
            for index,image in enumerate(manifest['images']):
                guard(32*1024**2)
                # Check exact mapped RGB tensor against all eight frozen C3 inputs.
                from detectron2.data.detection_utils import read_image
                mapped,dims=transtic_adapter.mapper_bgr(read_image(image['path'],format='BGR'))
                expected=plan['mapped_inputs'][str(image['image_id'])]
                assert hashlib.sha256(np.ascontiguousarray(mapped[...,::-1]).tobytes()).hexdigest()==expected['mapped_rgb_uint8_sha256']
                assert state['codec_attempts']<80
                state['codec_attempts']+=1;guard()
                t=time.monotonic();rgb,row=codec.reconstruct(image,out);torch.cuda.synchronize()
                elapsed=time.monotonic()-t
                assert rgb.dtype==torch.float32 and tuple(rgb.shape)==(3,*dims.detector_hw)
                assert bool(torch.isfinite(rgb).all()) and float(rgb.min())>=0 and float(rgb.max())<=1
                row['mapped_rgb_uint8_sha256']=expected['mapped_rgb_uint8_sha256']
                row['codec_wall_seconds']=elapsed
                # Freeze ties-to-even before observing any external AP.
                arr=torch.round(rgb.clamp(0,1)*255).to(torch.uint8).permute(1,2,0).cpu().numpy()
                png=out/f"reconstruction_{image['image_id']}_uint8.png"
                Image.fromarray(arr,'RGB').save(png)
                with Image.open(png) as decoded: assert np.array_equal(np.asarray(decoded),arr)
                float_path=Path(row['reconstruction_path'])
                loaded=np.load(float_path,allow_pickle=False)
                assert np.array_equal(loaded,rgb.cpu().numpy())
                row.update(uint8_path=str(png),uint8_sha256=sha(png),input_manifest_sha256=plan['manifest_sha256'])
                rows.append(row);write(out/'per_image.json',rows)
                state['completed']+=1;guard()
                if q==1 and index==0:
                    projection=(time.monotonic()-started)+elapsed*79
                    receipt={'first_manifest_image':True,'reuse_in_full_run':True,'codec_wall_seconds':elapsed,'projected_codec_worker_seconds':projection,'passed':projection<2400,'finished_unix':time.time()}
                    write(private/(method+'_pilot.json'),receipt)
                    assert receipt['passed'],'Pilot exceeds allocated worker budget'
                print(method,q,len(rows),'/20',flush=True)
            write(out/'status.json',{'status':'complete','codec_images':20})
            del codec;torch.cuda.empty_cache()
        state['status']='complete'
    except BaseException as error:
        state.update(status='failed',error_type=type(error).__name__,error=str(error));raise
    finally:
        state['gpu_seconds']=time.monotonic()-started;write(status_path,state)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--method',choices=['adapt_icmh','transtic'],required=True)
    a=p.parse_args();run(a.root,a.method)
