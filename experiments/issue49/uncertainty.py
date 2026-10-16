"""Paired delete-one pooled COCO evaluation; descriptive ranges, no AP CI.

Run only after all conditions are frozen and evaluated. No averaging image AP.
Predictions and IDs remain in private output; sanitized summaries may be public.
"""
import argparse
import contextlib
import io
import json
from pathlib import Path


def calculate(manifest_path, runs, output):
    from c3_neural_compression.evaluation import shared_detector
    manifest=json.loads(Path(manifest_path).read_text());ids=manifest['image_ids']
    if len(ids)<2:raise ValueError('Need at least two images')
    annotations=json.loads(Path(manifest['annotation_file']).read_text())
    support={i:set() for i in ids}
    for row in annotations['annotations']:
        if row['image_id'] in support and not row.get('iscrowd',0):support[row['image_id']].add(row['category_id'])
    records=[]
    for name,path in runs.items():
        directory=Path(path)
        if json.loads((directory/'status.json').read_text()).get('status')!='complete':raise ValueError('Incomplete run')
        if json.loads((directory/'manifest.json').read_text())['image_ids']!=ids:raise ValueError('Run manifest mismatch')
        predictions=json.loads((directory/'predictions.json').read_text())
        if any(p['image_id'] not in ids for p in predictions):raise ValueError('Unknown prediction image ID')
        for omitted in ids:
            subset=[i for i in ids if i!=omitted]
            kept=[p for p in predictions if p['image_id']!=omitted]
            with contextlib.redirect_stdout(io.StringIO()):
                metrics=shared_detector.evaluate_coco(manifest['annotation_file'],kept,subset)
            cats=set().union(*(support[i] for i in subset))
            records.append({'condition':name,'omitted_image_id':omitted,'supported_category_count':len(cats),'metrics':metrics})
    values={name:[r['metrics']['box_ap'] for r in records if r['condition']==name] for name in runs}
    summary={'method':'paired delete-one official pooled COCO AP','N':len(ids),
             'interpretation':'descriptive sensitivity ranges; changing category support; not confidence intervals or superiority evidence',
             'AP_ranges_percent':{name:[100*min(v),100*max(v)] for name,v in values.items()},
             'category_support_range':[min(r['supported_category_count'] for r in records),max(r['supported_category_count'] for r in records)],
             'records':records}
    output=Path(output)
    with output.open('x') as stream:json.dump(summary,stream,indent=2,allow_nan=False);stream.write('\n')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',required=True)
    p.add_argument('--runs',required=True,help='JSON object condition name -> evaluation directory')
    p.add_argument('--output',required=True);a=p.parse_args();calculate(a.manifest,json.loads(Path(a.runs).read_text()),a.output)
