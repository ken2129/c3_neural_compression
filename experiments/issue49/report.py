"""Render a private, verified measurement bundle through a public whitelist.

This renderer does not certify experiment provenance. Before invoking it, the
collector must verify completed attempts, frozen settings, common evaluator,
input/output correspondence and official pooled COCO results. Missing rows and
undefined metrics stay explicit. Never feed fixtures to a research result PR.
"""
import argparse
import csv
import json
import math
from pathlib import Path

METHODS=('c3_image','c3_combined')
METRICS=('box_ap','box_ap50','box_ap75','box_ap_small','box_ap_medium','box_ap_large')
STATUSES={'complete','failed','stopped','not_measured'}


def number(value, *, minimum=0):
    if type(value) not in (int,float) or not math.isfinite(value) or value<minimum:
        raise ValueError('Invalid numerical result')
    return float(value)


def metrics(raw):
    result={}
    for key in METRICS:
        value=number(raw[key],minimum=-1)
        if value!=-1 and not 0<=value<=1:raise ValueError('Invalid COCO metric scale')
        result[key+'_percent']=None if value==-1 else 100*value
    return result


def public_report(private):
    if private.get('schema_version')!=1:raise ValueError('Unsupported schema')
    targets=[number(t) for t in private['targets']]
    if len(targets)!=4 or len(set(targets))!=4 or any(t==0 for t in targets):
        raise ValueError('Need four distinct positive targets')
    phases=private['phases'];sets=[];public={}
    for phase in ('calibration','heldout'):
        source=phases[phase];ids=source['image_ids']
        if not ids or len(set(ids))!=len(ids):raise ValueError('Invalid image set')
        sets.append(set(ids));N=len(ids)
        if phase=='calibration' and N!=20:raise ValueError('Calibration must use fixed20')
        rows=source['points'];expected={(m,t) for m in METHODS for t in targets}
        if len(rows)!=8 or {(r['method'],r['target_bpp']) for r in rows}!=expected:
            raise ValueError('All eight conditions must be present, including failures')
        clean=[]
        for row in rows:
            if row['status'] not in STATUSES:raise ValueError('Invalid measurement status')
            value={'method':row['method'],'target_bpp':row['target_bpp'],'status':row['status'],
                   'mean_estimated_bpp':None,**{k+'_percent':None for k in METRICS},'target_relative_error':None,'target_passed':None}
            if row['status']=='complete':
                if row['image_count']!=N or row.get('official_pooled_COCO') is not True:
                    raise ValueError('Complete row must certify full-set official pooled COCO')
                rate=number(row['mean_estimated_bpp']);target=row['target_bpp']
                if rate==0:raise ValueError('Invalid measured rate')
                value.update(mean_estimated_bpp=rate,**metrics(row['metrics']),
                             target_relative_error=abs(rate-target)/target,target_passed=abs(rate-target)/target<=.1)
            clean.append(value)
        baseline=source['baseline']
        if baseline['status'] not in STATUSES:raise ValueError('Invalid baseline status')
        base={'status':baseline['status'],'input':'COCO-distributed JPEG',**{k+'_percent':None for k in METRICS}}
        if baseline['status']=='complete':
            if baseline['image_count']!=N or baseline.get('official_pooled_COCO') is not True:
                raise ValueError('Baseline must certify full-set official pooled COCO')
            base.update(metrics(baseline['metrics']))
        pairs=[]
        for target in targets:
            normal=next(r for r in clean if r['method']=='c3_image' and r['target_bpp']==target)
            combined=next(r for r in clean if r['method']=='c3_combined' and r['target_bpp']==target)
            pair={'target_bpp':target,'relative_rate_difference':None,'rate_pair_passed':None,'AP_difference_points':None}
            if normal['status']==combined['status']=='complete':
                gap=abs(combined['mean_estimated_bpp']-normal['mean_estimated_bpp'])/normal['mean_estimated_bpp']
                a=normal['box_ap_percent'];b=combined['box_ap_percent']
                pair.update(relative_rate_difference=gap,rate_pair_passed=gap<=.1,
                            AP_difference_points=None if a is None or b is None else b-a)
            pairs.append(pair)
        public[phase]={'N':N,'baseline':base,'points':clean,'paired_comparisons':pairs}
    if sets[0]&sets[1]:raise ValueError('Calibration and heldout sets overlap')
    return {'schema_version':1,'targets':targets,'phases':public,
            'costs':{'GPU_hours':number(private['costs']['gpu_seconds'])/3600,
                     'wall_hours':number(private['costs']['wall_seconds'])/3600},
            'rate_definition':'mean(total estimated latent and per-image network bits / resized nonpadding pixels)',
            'interpretation':'Measured points only. AP differences at mismatched rates are not strict equal-rate improvements. No AP interpolation, BD-rate or statistical superiority claim.',
            'uncertainty_method':'paired delete-one official pooled COCO AP, descriptive ranges with category support changes; not confidence intervals'}


def render(public, output):
    output=Path(output);output.mkdir(exist_ok=False,parents=True)
    (output/'comparison.json').write_text(json.dumps(public,indent=2,allow_nan=False)+'\n')
    rows=[dict(phase=phase,**r) for phase in ('calibration','heldout') for r in public['phases'][phase]['points']]
    with (output/'comparison.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,2,figsize=(10,4),sharey=True)
    for ax,phase in zip(axes,('calibration','heldout')):
        data=public['phases'][phase]
        for method,label,marker in [('c3_image','C3','o'),('c3_combined','C3 prime','s')]:
            points=sorted([r for r in data['points'] if r['method']==method and r['status']=='complete' and r['box_ap_percent'] is not None],key=lambda r:r['mean_estimated_bpp'])
            if points:ax.scatter([r['mean_estimated_bpp'] for r in points],[r['box_ap_percent'] for r in points],label=label,marker=marker)
        baseline=data['baseline']['box_ap_percent']
        if baseline is not None:ax.axhline(baseline,color='0.4',ls='--',label='JPEG baseline')
        missing=sum(r['status']!='complete' for r in data['points'])
        if missing:ax.text(.03,.03,f'{missing}/8 conditions incomplete',transform=ax.transAxes)
        ax.set_title(f'{phase.capitalize()} (N={data["N"]})');ax.set_xlabel('Mean estimated bpp');ax.grid(alpha=.2)
        handles,labels=ax.get_legend_handles_labels()
        if handles:ax.legend()
    axes[0].set_ylabel('Official pooled COCO box AP (%)');fig.tight_layout()
    fig.savefig(output/'comparison.png',dpi=180);fig.savefig(output/'comparison.pdf');plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('private_bundle');p.add_argument('output');a=p.parse_args()
    render(public_report(json.loads(Path(a.private_bundle).read_text())),a.output)
