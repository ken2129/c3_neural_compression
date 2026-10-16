"""Publish only aggregate research values after all 16 conditions are audited."""
import argparse
import csv
import json
import math
from pathlib import Path

METRICS=('box_ap','box_ap50','box_ap75','box_ap_small','box_ap_medium','box_ap_large')


def create(private,output):
    private=Path(private);output=Path(output)
    plan=json.loads((private/'preflight.json').read_text())
    points=[]
    for method in ('adapt_icmh','transtic'):
        audit=json.loads((private/(method+'_audit.json')).read_text());assert audit['status']=='passed'
        for point in audit['summaries']:
            points.append({'method':method,'quality':point['quality'],'precision':point['precision'],'bpp':point['bpp'],
                **{k:100*point['metrics'][k] for k in METRICS},
                'delete_one_AP_min':point['delete_one_AP_percent_range'][0],
                'delete_one_AP_max':point['delete_one_AP_percent_range'][1]})
    uncertainty=json.loads((private/'all_uncertainty.json').read_text())
    for name,record in plan['c3_runs'].items():
        directory=Path(record['evaluation']);rows=json.loads((directory/'per_image.json').read_text());assert len(rows)==20
        metrics=json.loads((directory/'metrics.json').read_text());bpp=math.fsum(r['estimated_bits']['total']/math.prod(r['detector_hw']) for r in rows)/20
        lo,hi=uncertainty['AP_ranges_percent'][name]
        points.append({'method':'c3_prime' if '_combined_' in name else 'c3','quality':int(name.split('_')[-1]),'precision':'uint8','bpp':bpp,
            **{k:100*metrics[k] for k in METRICS},'delete_one_AP_min':lo,'delete_one_AP_max':hi})
    baseline=json.loads((Path(plan['baseline'])/'metrics.json').read_text())
    ranges={m:[min(p['bpp'] for p in points if p['method']==m),max(p['bpp'] for p in points if p['method']==m)] for m in ('c3','c3_prime','transtic','adapt_icmh')}
    overlap=[max(v[0] for v in ranges.values()),min(v[1] for v in ranges.values())]
    receipts=[json.loads(p.read_text()) for p in (private/'gpu_receipts').glob('*.json')]
    assert len(receipts)==4 and all(r['status']=='complete' for r in receipts)
    assert sum(r['gpu_seconds'] for r in receipts)<7200
    statuses=[json.loads((private/(m+'_codec_status.json')).read_text()) for m in ('adapt_icmh','transtic')]
    assert all(v['status']=='complete' and v['codec_attempts']==80 and v['completed']==80 for v in statuses)
    costs={'gpu_seconds_including_startup_and_exit':sum(r['gpu_seconds'] for r in receipts),
           'codec_attempts':sum(v['codec_attempts'] for v in statuses),'new_artifact_bytes_at_report':sum(p.stat().st_size for p in private.rglob('*') if p.is_file())}
    assert costs['new_artifact_bytes_at_report']<4*1024**3
    data={'image_count':20,'rate':'mean estimated y+z bits / resized nonpadding pixels; C3 includes per-image networks',
          'precision_primary':'external clamp float32; C3 native uint8',
          'precision_secondary':'external ties-to-even rounded uint8; same codec reconstruction and bits',
          'codec_alignment':{'transtic':'official 256-multiple bilinear resize before/after codec, align_corners=False; user-approved exception','adapt_icmh':'256-multiple right/bottom zero pad then crop','c3':'native detector-sized input'},
          'points':points,'JPEG_baseline':{k:100*baseline[k] for k in METRICS},'rate_ranges':ranges,
          'four_way_rate_overlap':overlap if overlap[0]<=overlap[1] else None,
          'costs':costs,'category_support_range':uncertainty['category_support_range'],
          'inference':'measured points only; descriptive delete-one sensitivity, not confidence intervals; no AP interpolation or superiority claim'}
    paired={}
    for m in ('adapt_icmh','transtic'):
        for q in range(1,5):
            f={r['omitted_image_id']:r['metrics']['box_ap'] for r in uncertainty['records'] if r['condition']==f'{m}_{q}_float32'}
            u={r['omitted_image_id']:r['metrics']['box_ap'] for r in uncertainty['records'] if r['condition']==f'{m}_{q}_uint8'}
            assert set(f)==set(u) and len(f)==20
            deltas=[100*(u[i]-f[i]) for i in f]
            paired[f'{m}_{q}']=[min(deltas),max(deltas)]
    data['paired_delete_one_uint8_minus_float_AP_pp_ranges']=paired
    import itertools
    data['pairwise_rate_range_overlap']={a+' / '+b:([max(ranges[a][0],ranges[b][0]),min(ranges[a][1],ranges[b][1])] if max(ranges[a][0],ranges[b][0])<=min(ranges[a][1],ranges[b][1]) else None) for a,b in itertools.combinations(ranges,2)}
    output.mkdir(parents=True,exist_ok=True)
    (output/'issue50_four_method.json').write_text(json.dumps(data,indent=2,allow_nan=False)+'\n')
    with (output/'issue50_four_method.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(points[0]),lineterminator='\n');w.writeheader();w.writerows(points)
        lo,hi=uncertainty['AP_ranges_percent']['baseline']
        w.writerow({'method':'JPEG_baseline','quality':'','precision':'original_JPEG','bpp':'',**{k:100*baseline[k] for k in METRICS},'delete_one_AP_min':lo,'delete_one_AP_max':hi})
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    styles={'c3':('C3','#1565c0','o'),'c3_prime':('C3′','#c62828','s'),'transtic':('TransTIC','#2e7d32','^'),'adapt_icmh':('Adapt-ICMH','#6a1b9a','D')}
    fig,axes=plt.subplots(1,2,figsize=(11,4.5),sharey=True)
    for ax,precision,title in zip(axes,('float32','uint8'),('Primary: external float32 / C3 uint8','Secondary: all uint8')):
        for method,(label,color,marker) in styles.items():
            selected=sorted((p for p in points if p['method']==method and p['precision']==('uint8' if method in ('c3','c3_prime') else precision)),key=lambda p:p['bpp'])
            assert len(selected)==4
            # Mark observations; lines only guide the eye, never estimated AP.
            ax.plot([p['bpp'] for p in selected],[p['box_ap'] for p in selected],marker=marker,color=color,label=label,linewidth=1)
            for p in selected:
                ax.vlines(p['bpp'],p['delete_one_AP_min'],p['delete_one_AP_max'],colors=color,alpha=.25,linewidth=2)
        ax.axhline(data['JPEG_baseline']['box_ap'],color='gray',linestyle='--',label='COCO JPEG baseline')
        ax.set(xlabel='Mean estimated bits / resized nonpadding pixel',title=title);ax.grid(alpha=.2)
    axes[0].set_ylabel('Pooled COCO box AP (%)');axes[1].legend(fontsize=8)
    fig.text(.5,.01,'20 images; ranges: delete-one sensitivity, not CIs. TransTIC resizes at codec alignment; Adapt pads/crops.',ha='center',fontsize=8)
    fig.tight_layout(rect=(0,.035,1,1))
    for extension in ('png','pdf'):fig.savefig(output/f'issue50_four_method.{extension}',dpi=180)
    plt.close(fig)
    print('Public aggregate files created with 16 primary points, 8 external secondary points and JPEG baseline')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--private',required=True);p.add_argument('--output',required=True);a=p.parse_args();create(a.private,a.output)
