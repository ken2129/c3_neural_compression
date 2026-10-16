"""Requirement-scoped local completion audit before publishing the result PR."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
from codec import write
from preflight import sha


def read(p):return json.loads(Path(p).read_text())


def audit(root):
    root=Path(root).resolve();private=root/'var/issue50_evaluation_20261016';work=root/'var/issue50_workspace'
    plan=read(private/'preflight.json');manifest=read(plan['manifest']);ids=manifest['image_ids']
    assert sha(plan['manifest'])==plan['manifest_sha256'] and len(ids)==20
    assert read(private/'alignment_decision.json')['selected']=='official_resize'
    for name,digest in plan['runtime_source_sha256'].items():assert sha(Path(plan['runtime'])/name)==digest
    all_unc=read(private/'all_uncertainty.json');assert len(all_unc['records'])==500 and len(all_unc['AP_ranges_percent'])==25
    indexed={(r['condition'],r['omitted_image_id']):r for r in all_unc['records']}
    assert len(indexed)==500
    for name in all_unc['AP_ranges_percent']:
        assert {i for n,i in indexed if n==name}==set(ids)
        for omit in ids:
            record=indexed[name,omit];m=record['metrics']
            assert m['image_ids']==[i for i in ids if i!=omit] and m['image_count']==19
            assert m['official_cocoeval_executed'] and m['iou_type']=='bbox'
    rows_by_condition={};codec_count=0
    for method in ('adapt_icmh','transtic'):
        status=read(private/(method+'_codec_status.json'));assert status['status']=='complete' and status['completed']==status['codec_attempts']==80
        codec_count+=status['codec_attempts']
        assert read(private/(method+'_evaluation_status.json'))['completed']==8
        a=read(private/(method+'_audit.json'));assert a['status']=='passed'
        for record in a['records']:assert record==indexed[record['condition'],record['omitted_image_id']]
        assert read(private/(method+'_pilot.json'))['passed']
        for q in range(1,5):
            d=private/'results'/f'{method}_{q}';assert read(d/'status.json')['status']=='complete'
            config=read(d/'config.json');expected=next(a for a in plan['assets'] if a['method']==method and a['quality']==q)
            assert config==expected and sha(config['asset']['checkpoint'])==config['asset']['sha256']
            assert read(d/'manifest.json')==manifest
            rows=read(d/'per_image.json');assert [r['image_id'] for r in rows]==ids
            for r in rows:
                assert r['mapped_rgb_uint8_sha256']==plan['mapped_inputs'][str(r['image_id'])]['mapped_rgb_uint8_sha256']
                assert sha(r['reconstruction_path'])==r['reconstruction_sha256'] and sha(r['uint8_path'])==r['uint8_sha256']
                if method=='transtic':assert r['alignment_equal_independent'] is True
            rows_by_condition[method,q]=rows
    assert codec_count==160
    old_unc=read(root/'var/issue49_evaluation_20261015/heldout_uncertainty.json')
    for record in old_unc['records']:
        name=record['condition'];name=name.replace('c3_image_','heldout_image_').replace('c3_combined_','heldout_combined_')
        other=indexed[name,record['omitted_image_id']]
        assert record['metrics']==other['metrics'] and record['supported_category_count']==other['supported_category_count']
    assert read(private/'c3_baseline_metric_audit.json')['status']=='passed'
    public=read(work/'docs/research/assets/issue50_four_method.json');assert len(public['points'])==24
    csv_rows=list(csv.DictReader((work/'docs/research/assets/issue50_four_method.csv').open()));assert len(csv_rows)==25 and csv_rows[-1]['method']=='JPEG_baseline'
    for p in public['points']:
        if p['method'] in ('transtic','adapt_icmh'):
            rows=rows_by_condition[p['method'],p['quality']];metrics=read(private/'evaluations'/f"{p['method']}_{p['quality']}_{p['precision']}"/'metrics.json')
        else:
            name=f"heldout_{'combined' if p['method']=='c3_prime' else 'image'}_{p['quality']}";directory=Path(plan['c3_runs'][name]['evaluation']);rows=read(directory/'per_image.json');metrics=read(directory/'metrics.json')
        expected=math.fsum(r['estimated_bits']['total']/math.prod(r['detector_hw']) for r in rows)/20
        assert p['bpp']==expected
        for key in ('box_ap','box_ap50','box_ap75','box_ap_small','box_ap_medium','box_ap_large'):assert p[key]==metrics[key]*100
    assert all(read(p)['status']=='complete' for p in (private/'gpu_receipts').glob('*.json'))
    gpu=sum(read(p)['gpu_seconds'] for p in (private/'gpu_receipts').glob('*.json'));assert gpu<7200
    changed=list((work/'experiments/issue50').glob('*.py'))+[work/'experiments/issue50/README.md',work/'docs/research/2026-10-16_issue50.md',work/'docs/research/2026-10-16_issue50_completion_audit.md']+list((work/'docs/research/assets').glob('issue50_four_method.*'))
    for path in changed:
        if path.suffix in ('.png','.pdf'):continue
        text=path.read_text();assert str(root) not in text
        assert not re.search(r'\b(?:'+ '|'.join(map(str,ids))+r')\b',text),'Private ID in public text'
    artifact_bytes=sum(p.stat().st_size for p in private.rglob('*') if p.is_file())+sum(p.stat().st_size for p in changed)
    assert artifact_bytes<4*1024**3 and shutil.disk_usage(root).free>3*1024**3
    result={'status':'passed','scope':'scientific/local delivery; PR publication audited separately','codec_attempts':160,'full_AP_replays':25,'paired_delete_one_replays':500,'all_replayed_records_exact':True,'all_public_points_match_bits_and_metrics':True,'public_csv_rows_including_JPEG':25,'gpu_seconds':gpu,'new_private_and_public_bytes':artifact_bytes,'free_bytes':shutil.disk_usage(root).free,'source_runtime_unchanged':True,'official_resize_user_exception_applied':True,'private_identifier_scan':'passed','files':{str(p.relative_to(work)):sha(p) for p in changed}}
    write(private/'final_scientific_audit.json',result)
    print('PASS:160 codec;25 full AP;500 paired subsets; all public numeric rows exact; budgets and privacy verified')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);audit(p.parse_args().root)
