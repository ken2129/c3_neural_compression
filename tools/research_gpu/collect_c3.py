"""CPU-only allowlist export for a single completed C3 comparison attempt.

Use as a pinned collect_argv. It creates public numerical data, never copies
the original private metrics, image IDs, paths, checksums, params or images.
"""
import argparse
import json
import math
from pathlib import Path


def collect(codec_root):
    from c3_neural_compression.evaluation.comparison_attempt import validate_completed_attempt
    roots = [path.parent for path in Path(codec_root).rglob('comparison_attempt.json')]
    if len(roots) != 1:
        raise ValueError('exactly one C3 attempt required')
    root = roots[0]
    ledger = validate_completed_attempt(root)
    rows = []
    signatures = None
    for record in ledger['outputs']:
        metrics = json.loads((root/record['metrics_path']).read_text())
        signature = metrics['experiment_signature']['fields']
        if signatures is not None and signature != signatures:
            raise ValueError('mixed C3 conditions')
        signatures = signature
        bits = metrics['estimated_bits']
        total = sum(bits[key] for key in ('latents','synthesis_network','entropy_network'))
        h, w, channels = metrics['input_shape']
        if (channels != 3 or not math.isclose(total,bits['total'],rel_tol=1e-6)
                or not math.isclose(total/(h*w),metrics['estimated_bpp']['total'],rel_tol=1e-6)
                or metrics['actual_bitstream_generated'] is not False):
            raise ValueError('C3 rate contract mismatch')
        row = dict(estimated_bpp=total/(h*w), psnr_quantized_db=metrics['psnr_quantized'],
                   optimization_seconds=metrics['timing_seconds']['optimization'],
                   quantization_search_seconds=metrics['timing_seconds']['network_quantization_search'])
        if any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in row.values()):
            raise ValueError('invalid C3 numerical output')
        if signature['machine_weight'] > 0:
            machine = metrics['machine_loss']
            if (machine['detector_checksum_before'] != machine['detector_checksum_after']
                    or machine['detector_gradients_none'] is not True):
                raise ValueError('C3 teacher changed')
            row['feature_L1'] = machine['distortion_total_uint8']
            if (type(row['feature_L1']) not in (int,float)
                    or not math.isfinite(row['feature_L1']) or row['feature_L1']<0):
                raise ValueError('invalid C3 feature loss')
        rows.append(row)
    if not rows:
        raise ValueError('empty C3 result')
    return dict(schema='c3-public-numerical-result-v1', image_count=len(rows),
        input_protocol=signatures.get('codec_input_protocol','original_rgb'),
        noise_steps=signatures['num_noise_steps'], max_STE_steps=signatures['max_num_ste_steps'],
        image_weight=signatures['image_weight'], machine_weight=signatures['machine_weight'],
        rd_weight=signatures['rd_weight'], actual_bitstream_generated=False,
        AP_measured=False, rows=rows,
        limitations=['Single installed condition; matching lambda is not matching rate.',
                    'Codec timings do not independently separate load/JIT/noise/STE/final saving.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('codec_root',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    result=collect(args.codec_root)
    text=json.dumps(result,indent=2,allow_nan=False)+'\n'
    if len(text.encode())>12000:
        raise ValueError('C3 public export too large for the installed IPC contract')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:
        stream.write(text)


if __name__=='__main__':main()
