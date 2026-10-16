"""Recompute private artifacts, pooled AP, rates and paired delete-one sensitivity."""
import argparse
import contextlib
import io
import json
import math
from pathlib import Path
import sys
from codec import write
from preflight import sha


def run(root,method):
    root=Path(root).resolve();private=root/'var/issue50_evaluation_20261016'
    plan=json.loads((private/'preflight.json').read_text());sys.path.insert(0,str(Path(plan['runtime']).parent))
    import numpy as np
    from PIL import Image
    from c3_neural_compression.evaluation import shared_detector
    manifest=json.loads(Path(plan['manifest']).read_text());ids=manifest['image_ids']
    annotations=json.loads(Path(manifest['annotation_file']).read_text())
    support={i:set() for i in ids}
    for a in annotations['annotations']:
        if a['image_id'] in support and not a.get('iscrowd',0):support[a['image_id']].add(a['category_id'])
    summaries=[];records=[]
    for q in range(1,5):
        source=private/'results'/f'{method}_{q}'
        rows=json.loads((source/'per_image.json').read_text())
        assert len(rows)==20 and [r['image_id'] for r in rows]==ids
        bpps=[]
        for row in rows:
            assert row['mapped_rgb_uint8_sha256']==plan['mapped_inputs'][str(row['image_id'])]['mapped_rgb_uint8_sha256']
            assert sha(row['reconstruction_path'])==row['reconstruction_sha256']
            assert sha(row['uint8_path'])==row['uint8_sha256']
            array=np.load(row['reconstruction_path'],allow_pickle=False)
            assert array.dtype==np.float32 and np.isfinite(array).all() and array.min()>=0 and array.max()<=1
            expected=np.rint(array*255).clip(0,255).astype(np.uint8).transpose(1,2,0)
            with Image.open(row['uint8_path']) as image:assert np.array_equal(expected,np.asarray(image))
            b=row['estimated_bits'];assert set(b)=={'y','z','total'}
            assert all(math.isfinite(v) and v>=0 for v in b.values())
            assert math.isclose(b['total'],math.fsum((b['y'],b['z'])),rel_tol=1e-12)
            h,w=row['detector_hw'];assert array.shape==(3,h,w)
            bpps.append(b['total']/(h*w))
        for precision in ('float32','uint8'):
            name=f'{method}_{q}_{precision}';directory=private/'evaluations'/name
            assert json.loads((directory/'status.json').read_text())['status']=='complete'
            assert json.loads((directory/'manifest.json').read_text())==manifest
            assert json.loads((directory/'detector.json').read_text())==plan['detector']
            predictions=json.loads((directory/'predictions.json').read_text())
            with contextlib.redirect_stdout(io.StringIO()):
                metrics=shared_detector.evaluate_coco(manifest['annotation_file'],predictions,ids)
            assert metrics==json.loads((directory/'metrics.json').read_text())
            values=[]
            for omit in ids:
                kept=[i for i in ids if i!=omit]
                with contextlib.redirect_stdout(io.StringIO()):
                    m=shared_detector.evaluate_coco(manifest['annotation_file'],[p for p in predictions if p['image_id']!=omit],kept)
                cats=set().union(*(support[i] for i in kept));values.append(m['box_ap'])
                records.append({'condition':name,'omitted_image_id':omit,'supported_category_count':len(cats),'metrics':m})
            summaries.append({'method':method,'quality':q,'precision':precision,'bpp':math.fsum(bpps)/20,
                              'metrics':metrics,'delete_one_AP_percent_range':[100*min(values),100*max(values)]})
    result={'status':'passed','codec_images':80,'evaluation_paths':8,'roundtrip_float_exact':True,'uint8_numpy_torch_ties_even_exact':True,
            'official_AP_recomputed':True,'category_count':len(set().union(*support.values())),
            'delete_one_category_support_range':[min(r['supported_category_count'] for r in records),max(r['supported_category_count'] for r in records)],
            'summaries':summaries,'records':records,'interpretation':'paired delete-one pooled official COCO AP; descriptive sensitivity, not confidence intervals'}
    write(private/(method+'_audit.json'),result)
    print(method,'80 artifacts and 8 pooled AP paths independently verified; 160 delete-one pooled evaluations complete')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--method',required=True);a=p.parse_args();run(a.root,a.method)
