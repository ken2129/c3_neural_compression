"""Run fixed C3 with bounded storage; numerical optimization is unchanged.

The private job JSON supplies manifest, variant, output_dir and evidence_dir.
A separate supervisor must enforce GPU/time/disk limits. Evidence is stored
outside the strict comparison output root, so existing attempt validation stays
unchanged. This runner never resumes/retries or transfers data automatically.
"""
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')
    temporary.replace(path)


def main(job_path):
    job = json.loads(Path(job_path).read_text())
    # Approval is deliberately not inferred from a prior experiment's budget.
    approval = json.loads(Path(job['scope_approval_file']).read_text())
    if approval.get('approved') is not True or approval.get('plan_sha256') != file_hash(job['scope_plan_file']):
        raise ValueError('Missing approval for this exact scope plan')
    if os.environ.get('CUDA_VISIBLE_DEVICES') not in {'0', '1'}:
        raise ValueError('Supervisor must select an explicitly approved GPU')
    if '--xla_gpu_exclude_nondeterministic_ops=true' not in os.environ.get('XLA_FLAGS','').split():
        raise ValueError('Declared XLA deterministic flag is required before imports')
    import numpy as np
    import jax
    import torch
    from c3_neural_compression.experiments import image
    from c3_neural_compression.evaluation import comparison as common
    from c3_neural_compression.evaluation import shared_detector
    torch.backends.cudnn.deterministic = True
    if jax.devices()[0].platform != 'gpu':
        raise RuntimeError('GPU runner was not started with a GPU backend')
    manifest = common.read_json(job['manifest']); common.validate_assets(manifest)
    evidence = Path(job['evidence_dir']).resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    output = Path(job['output_dir']).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Fresh attempt requires an empty output root')
    os.environ['C3_ISSUE49_JOB'] = str(Path(job_path).resolve())
    original_fit = image.Experiment.fit_datum
    original_save = image.Experiment._save_datum_outputs
    original_checkpoint = image.Experiment._save_noise_checkpoint
    original_eval = image.Experiment.eval
    receipts = []
    def save_params(path, params):
        leaves = jax.tree_util.tree_leaves(params)
        if not all(np.isfinite(np.asarray(leaf)).all() for leaf in leaves):
            raise RuntimeError('Nonfinite parameters')
        temporary = path.with_suffix('.tmp')
        with temporary.open('wb') as stream:
            pickle.dump(jax.device_get(params), stream, protocol=pickle.HIGHEST_PROTOCOL)
            stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
        return {'sha256':file_hash(path), 'bytes':path.stat().st_size}
    def checkpoint(self, params, opt_state, rng, next_step):
        if not all(np.isfinite(np.asarray(x)).all() for x in jax.tree_util.tree_leaves((params,opt_state))):
            raise RuntimeError('Nonfinite checkpoint')
        original_checkpoint(self, params, opt_state, rng, next_step)
        directory = Path(self._checkpoint_directory()).resolve()
        assert directory.is_relative_to(evidence/'checkpoints')
        current = directory/f'noise_step_{next_step:09d}.pkl'
        with current.open('rb') as stream:
            state = pickle.load(stream)
        assert state['next_step'] == next_step and state['objective_signature'] == self._objective_signature()
        record = {'datum_index':self._current_datum_index, 'step':next_step,
                  'sha256':file_hash(current), 'bytes':current.stat().st_size,
                  'rng':np.asarray(rng).tolist()}
        receipts.append(record); write_json(evidence/'checkpoint_receipts.json',receipts)
        # Only this runner's earlier checkpoints, after replacement validation.
        for old in directory.glob('noise_step_*.pkl'):
            if old != current:
                old.unlink()
    def fit(self, inputs, rng, datum_index=0):
        row = manifest['images'][datum_index]
        expected = shared_detector.expected_resized_shape(*row['original_hw'])
        if inputs.shape != (*expected,3) or not np.isfinite(inputs).all() or inputs.min()<0 or inputs.max()>1:
            raise ValueError('Input dimension/range mismatch')
        target = evidence/f'datum_{datum_index:05d}'; target.mkdir(exist_ok=False)
        write_json(target/'input_identity.json', {'image_id':row['image_id'],
            'JPEG_sha256':row['sha256'],'input_float32_sha256':hashlib.sha256(np.ascontiguousarray(inputs).tobytes()).hexdigest(),
            'shape':list(inputs.shape),'initial_datum_rng':np.asarray(rng).tolist()})
        params = original_fit(self, inputs, rng, datum_index=datum_index)
        write_json(target/'trained_params_receipt.json',save_params(target/'trained_params.pkl',params))
        return params
    def save(self, datum_index, inputs, quantized_params, metrics, timing, macs_per_pixel, datum_metadata=None):
        target = evidence/f'datum_{datum_index:05d}'
        record = save_params(target/'quantized_params.pkl', quantized_params)
        if not all(np.isfinite(float(value)) for value in metrics.values()):
            raise RuntimeError('Nonfinite final metric')
        original_save(self, datum_index, inputs, quantized_params, metrics, timing, macs_per_pixel, datum_metadata)
        saved = output/f'datum_{datum_index:05d}'
        row = json.loads((saved/'metrics.json').read_text())
        assert row['datum_metadata']['image_id'] == manifest['image_ids'][datum_index]
        assert all(row['estimated_bits'][key]>=0 for key in ('latents','synthesis_network','entropy_network','total'))
        rec = np.asarray(jax.device_get(self.reconstruct(quantized_params,inputs.shape[:-1])))
        from PIL import Image
        pixels = np.asarray(Image.open(saved/'reconstruction.png'))
        if not np.isfinite(rec).all() or not np.array_equal(pixels,np.clip(np.rint(rec*255),0,255).astype(np.uint8)):
            raise ValueError('Final reconstruction does not match retained parameters')
        record.update(PNG_sha256=file_hash(saved/'reconstruction.png'),metrics_sha256=file_hash(saved/'metrics.json'),
                      trained_params_sha256=file_hash(target/'trained_params.pkl'))
        write_json(target/'final_artifacts_verified.json',record)
        # Completed datum can be decoded from final params; log retains RNG/source.
        for own in (evidence/'checkpoints'/f'datum_{datum_index:05d}').glob('noise_step_*.pkl'):
            own.unlink()
    def evaluate(self,*args,**kwargs):
        metrics = original_eval(self,*args,**kwargs)
        if not all(np.isfinite(np.asarray(value)).all() for value in metrics.values()):
            raise RuntimeError('Nonfinite intermediate evaluation')
        return metrics
    image.Experiment._save_noise_checkpoint = checkpoint
    image.Experiment.fit_datum = fit
    image.Experiment._save_datum_outputs = save
    image.Experiment.eval = evaluate
    # Existing jaxline entrypoint fixes the original host/device RNG rules.
    sys.argv = [sys.argv[0], '--config='+str(Path(__file__).with_name('storage_config.py'))+':'+job['variant']]
    image.flags.mark_flag_as_required('config')
    image.app.run(image.functools.partial(image.platform.main,image.Experiment))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: storage_runner.py PRIVATE_JOB_JSON')
    main(sys.argv[1])
