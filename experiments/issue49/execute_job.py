"""Dispatch one separately budgeted codec, evaluation, or JPEG-baseline job."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main(path):
    job=json.loads(Path(path).read_text()); mode=job['mode']
    # Only the supervisor invokes this dispatcher after verifying exact job hashes.
    if mode=='codec':
        subprocess.run([sys.executable,str(Path(__file__).with_name('storage_runner.py')),path],check=True)
    elif mode=='evaluate':
        from c3_neural_compression.evaluation import comparison_runner
        comparison_runner.run(json.loads(Path(job['evaluation_config']).read_text()),job['evaluation_dir'])
    elif mode=='baseline':
        from c3_neural_compression.evaluation import comparison as common, shared_detector
        manifest=common.read_json(job['manifest']);common.validate_assets(manifest)
        output=Path(job['evaluation_dir']);output.mkdir(exist_ok=False)
        detector=shared_detector.SharedCocoDetector(job['detector']['checkpoint'],device='cuda:0',expected_sha256=job['detector']['sha256'])
        predictions=[]
        common.write_json(output/'manifest.json',manifest)
        common.write_json(output/'detector.json',detector.metadata())
        common.write_json(output/'status.json',{'status':'running'})
        for row in manifest['images']:
            rgb=shared_detector.resize_original_rgb(row['path'])
            predictions.extend(detector.predict(rgb,image_id=row['image_id'],original_height=row['original_hw'][0],original_width=row['original_hw'][1]))
            common.write_json(output/'predictions.json',predictions)
        common.write_json(output/'metrics.json',shared_detector.evaluate_coco(manifest['annotation_file'],predictions,manifest['image_ids']))
        common.write_json(output/'status.json',{'status':'complete','input':'COCO-distributed JPEG, not pre-JPEG lossless originals'})
    else:raise ValueError('Unknown job mode')


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: execute_job.py PRIVATE_JOB_JSON')
    main(sys.argv[1])
