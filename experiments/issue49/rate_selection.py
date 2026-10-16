"""Rate-only fixed20 calibration helpers implementing the Issue49 scope rule.

AP, seed quality and detector output are never inputs to candidate selection.
The observe command can audit existing results; it does not adopt their backend
or declare them compatible with new calibration measurements.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path


def positive(value, label):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value<=0:
        raise ValueError(label+' must be finite and positive')
    return float(value)


def select_candidate(target, candidates):
    target=positive(target,'target')
    if not candidates: raise ValueError('No measured candidate')
    # Preserve input attempt order for exact ties; never inspect extra fields.
    index=min(range(len(candidates)),key=lambda i:abs(math.log(positive(candidates[i]['mean_bpp'],'mean_bpp')/target)))
    chosen=candidates[index];positive(chosen['lambda'],'lambda')
    return {'candidate_index':index,'lambda':chosen['lambda'],'mean_bpp':chosen['mean_bpp'],
            'target_bpp':target,'target_relative_error':abs(chosen['mean_bpp']-target)/target,
            'target_tolerance_passed':abs(chosen['mean_bpp']-target)/target<=.1}


def correction(target, current_lambda, observations):
    target=positive(target,'target');current_lambda=positive(current_lambda,'lambda')
    # One observation per fresh condition, each a complete fixed20 mean.
    rows=[{'lambda':positive(r['lambda'],'lambda'),'mean_bpp':positive(r['mean_bpp'],'mean_bpp'),
           'source_index':i} for i,r in enumerate(observations)]
    if any(r.get('image_count')!=20 for r in observations):
        raise ValueError('Each calibration observation must contain all fixed20 images')
    rows.sort(key=lambda r:(r['mean_bpp'],r['source_index']))
    pairs=[(a,b) for a,b in zip(rows,rows[1:]) if a['mean_bpp']!=b['mean_bpp'] and a['lambda']!=b['lambda']]
    if not pairs: raise ValueError('No usable distinct pair; stop instead of inventing slope')
    def distance(pair):
        a,b=pair
        return 0 if a['mean_bpp']<=target<=b['mean_bpp'] else min(abs(math.log(target/a['mean_bpp'])),abs(math.log(target/b['mean_bpp'])))
    a,b=min(pairs,key=distance)
    f=math.log(target/a['mean_bpp'])/math.log(b['mean_bpp']/a['mean_bpp'])
    log_proposal=math.log(a['lambda'])+f*math.log(b['lambda']/a['lambda'])
    # Clamp in log space, including extreme extrapolation without overflow.
    capped_log=min(math.log(current_lambda)+math.log(2),max(math.log(current_lambda)-math.log(2),log_proposal))
    return {'lambda':math.exp(capped_log),'target_bpp':target,'source_indices':[a['source_index'],b['source_index']],
            'rule':'log-rate/log-lambda interpolation or nearest-pair extrapolation; factor2 update cap',
            'uncapped_log_lambda':log_proposal,'factor_cap_applied':capped_log!=log_proposal,
            'AP_used':False}


def tolerance_flags(target, normal_bpp, combined_bpp):
    target=positive(target,'target');normal_bpp=positive(normal_bpp,'normal rate');combined_bpp=positive(combined_bpp,'combined rate')
    normal_error=abs(normal_bpp-target)/target;combined_error=abs(combined_bpp-target)/target
    pair_error=abs(combined_bpp-normal_bpp)/normal_bpp
    return {'normal_target_relative_error':normal_error,'combined_target_relative_error':combined_error,
            'between_method_relative_difference':pair_error,'normal_target_passed':normal_error<=.1,
            'combined_target_passed':combined_error<=.1,'between_method_passed':pair_error<=.1,
            'between_method_denominator':'normal C3 measured mean bpp'}


def observe(manifest_path, output_root, method):
    from c3_neural_compression.evaluation import comparison as common
    manifest=common.read_json(manifest_path);common.validate_assets(manifest)
    found,signature=common.discover_c3(manifest,[output_root],method,input_protocol='detector_input_v1')
    if len(manifest['image_ids'])!=20: raise ValueError('Calibration requires fixed20, never a single-image proxy')
    rates=[]
    for i in manifest['image_ids']:
        metrics=found[i]['metrics'];shape=metrics['input_shape']
        rates.append(metrics['estimated_bits']['total']/(shape[0]*shape[1]))
    return {'method':method,'lambda':signature['fields']['rd_weight'],'mean_bpp':math.fsum(rates)/20,
            'image_count':20,'per_image_bpp':rates,'source_commit':signature['fields']['code_commit'],
            'manifest_sha256':common.sha256(manifest_path),
            'attempt_ledger_sha256':common.sha256(Path(output_root)/'comparison_attempt.json'),
            'role':'audited observation only; backend compatibility must be established before adoption','AP_used':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',required=True)
    p.add_argument('--output-root',required=True);p.add_argument('--method',choices=['c3_image','c3_combined'],required=True)
    p.add_argument('--output',required=True);a=p.parse_args();result=observe(a.manifest,a.output_root,a.method)
    with Path(a.output).open('x') as stream:json.dump(result,stream,indent=2,allow_nan=False);stream.write('\n')
    print(json.dumps({k:v for k,v in result.items() if k in {'method','mean_bpp','image_count','AP_used'}}))
