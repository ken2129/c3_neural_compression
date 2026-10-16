"""Read-only audit and private frozen plan for the Issue50 comparison."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def main(root):
    root = Path(root).resolve()
    private = root / 'var/issue50_evaluation_20261016'
    private.mkdir(exist_ok=True)
    runtime = root / 'var/issue49_runtime_20261015/c3_neural_compression'
    assert subprocess.check_output(['git', '-C', str(runtime), 'rev-parse', 'HEAD'], text=True).strip() == '0c3a60c703bd41956b6558d527470350bcc7dd5d'
    assert not subprocess.check_output(['git', '-C', str(runtime), 'diff', 'HEAD'], text=True).strip()
    sys.path.insert(0, str(runtime.parent))
    import numpy as np
    from PIL import Image
    from detectron2.data.detection_utils import read_image
    from c3_neural_compression.evaluation import comparison as common, detector_input_protocol as protocol, transtic_adapter
    heldout = root / 'var/issue49_evaluation_20261015'
    manifest_path = heldout / 'heldout_selection_v1/frozen_manifest.json'
    manifest = read(manifest_path)
    frozen = read(heldout / 'frozen_eight_conditions.json')
    assert sha(manifest_path) == frozen['heldout_manifest_sha256']
    assert len(manifest['image_ids']) == 20
    assert not set(manifest['image_ids']) & set(read(heldout / 'calibration_subset_ids.json'))
    common.validate_assets(manifest)
    mapped = {}
    for im in manifest['images']:
        bgr = read_image(im['path'], format='BGR')
        d2, dims = transtic_adapter.mapper_bgr(bgr)
        with Image.open(im['path']) as image:
            c3 = protocol.map_rgb_uint8(image.convert('RGB'))
        assert np.array_equal(d2[..., ::-1], c3), 'RGB/PIL versus D2 input mismatch'
        mapped[im['image_id']] = protocol.mapped_metadata(c3)
    runs = {}
    detector = None
    for method in ('image', 'combined'):
        for rate in (100, 150, 225, 300):
            name = f'heldout_{method}_{rate}'
            result = heldout / 'jobs' / name / 'results'
            evaluation = heldout / 'jobs' / (name + '_eval') / 'evaluation'
            attempt = read(result / 'comparison_attempt.json')
            assert attempt['status'] == 'completed'
            assert read(evaluation / 'status.json')['status'] == 'complete'
            assert read(evaluation / 'manifest.json') == manifest
            metadata = read(evaluation / 'detector.json')
            if detector is None: detector = metadata
            assert metadata == detector
            rows = read(evaluation / 'per_image.json')
            assert [r['image_id'] for r in rows] == manifest['image_ids']
            # Recompute official rate summary from original bits.
            assert protocol.rate_summary(manifest, rows, 'c3_' + method, 'detector_input_v1') == read(evaluation / 'rate_summary.json')
            for datum in attempt['outputs']:
                for key in ('config', 'metrics', 'reconstruction'):
                    assert sha(result / datum[key + '_path']) == datum[key + '_sha256']
                m = read(result / datum['metrics_path'])
                assert m['attempt_id'] == attempt['attempt_id']
                inp = m['datum_metadata']
                assert inp['mapped_rgb_uint8_sha256'] == mapped[datum['image_id']]['mapped_rgb_uint8_sha256']
                assert m['estimated_bits'] == rows[datum['datum_index']]['estimated_bits']
            runs[name] = {'evaluation': str(evaluation), 'attempt_sha256': sha(result / 'comparison_attempt.json')}
    baseline = heldout / 'jobs/heldout_baseline/evaluation'
    assert read(baseline / 'status.json')['status'] == 'complete'
    assert read(baseline / 'manifest.json') == manifest
    assert read(baseline / 'detector.json') == detector
    assets = []
    for method in ('transtic', 'adapt_icmh'):
        for q in range(1, 5):
            source = root / (f'var/issue20_resume_20261010_retry1/results/transtic_det{q}_eval/config.json' if method == 'transtic' else f'var/issue23_calibration20_20261010/results/adapt_icmh_level{q}_eval/config.json')
            config = read(source)
            asset = config[method]
            assert sha(asset['checkpoint']) == asset['sha256']
            repo = asset['repository']
            expected = transtic_adapter.UPSTREAM_COMMIT if method == 'transtic' else 'cf77f2eab798c9e949d2efd46db1adf3bbbc5cc1'
            assert subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip() == expected
            if method == 'transtic': transtic_adapter.verify_upstream(repo)
            else: assert not subprocess.check_output(['git', '-C', repo, 'diff', 'HEAD'], text=True).strip()
            assets.append({'method': method, 'quality': q, 'asset': asset, 'source_config_sha256': sha(source), 'source_commit': expected})
    plan = {'created_unix': time.time(), 'manifest': str(manifest_path), 'manifest_sha256': sha(manifest_path), 'runtime': str(runtime),
            'runtime_source_sha256': {str(p.relative_to(runtime)): sha(p) for p in sorted(runtime.rglob('*.py'))},
            'mapped_inputs': mapped, 'c3_runs': runs, 'baseline': str(baseline), 'detector': detector, 'assets': assets,
            'limits': {'codec_inferences':160, 'gpu_seconds':7200, 'artifact_bytes':4*1024**3, 'reserve_bytes':3*1024**3},
            'precision': {'primary':'clamp float32', 'secondary':'torch.round (ties to even) of clamp(0,1)*255, uint8 HWC RGB lossless PNG; read PNG and divide by255 in float32', 'frozen_before_external_AP':True},
            'transtic_alignment':'official_resize; user-approved exception 2026-10-16',
            'free_bytes':shutil.disk_usage(root).free}
    destination = private / 'preflight.json'
    with destination.open('x') as f: json.dump(plan, f, indent=2)
    print('Preflight passed: 160 C3 input hashes, 8 complete attempts, 8 public checkpoint hashes; precision frozen')


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);main(p.parse_args().root)
