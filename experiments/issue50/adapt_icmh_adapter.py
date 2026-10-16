"""Frozen official TIC-SFMA inference; no training or shared source changes."""
import importlib.util
import math
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import torch

from c3_neural_compression.evaluation import comparison as common
from c3_neural_compression.evaluation import shared_detector

COMMIT = 'cf77f2eab798c9e949d2efd46db1adf3bbbc5cc1'


class AdaptICMHAdapter:
    def __init__(self, repository, checkpoint, sha256, quality, device='cuda:0'):
        if quality not in (1, 2, 3, 4):
            raise ValueError('Expected quality 1–4')
        self.repository = Path(repository).resolve()
        self.checkpoint = Path(checkpoint).resolve()
        self.quality = quality
        self.device = device
        head = subprocess.check_output(
            ['git', '-C', str(self.repository), 'rev-parse', 'HEAD'], text=True).strip()
        if head != COMMIT or subprocess.check_output(
            ['git', '-C', str(self.repository), 'diff', 'HEAD'], text=True).strip():
            raise ValueError('Official Adapt-ICMH source is not the frozen unmodified commit')
        if common.sha256(self.checkpoint) != sha256:
            raise ValueError('Checkpoint SHA256 mismatch')
        self.sha256 = sha256
        sys.path.insert(0, str(self.repository / 'examples'))
        from models.tic_sfma import TIC_SFMA
        spec = importlib.util.spec_from_file_location(
            'adapt_official_alignment', self.repository / 'examples/utils/alignment.py')
        alignment = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alignment)
        self.alignment_class = alignment.Alignment
        torch.cuda.synchronize()
        start = time.perf_counter()
        checkpoint_data = torch.load(self.checkpoint, map_location='cpu', weights_only=True)
        params = checkpoint_data['state_dict']
        if params and all(key.startswith('module.') for key in params):
            params = {key[7:]: value for key, value in params.items()}
        self.model = TIC_SFMA(N=128, M=192)
        self.model.load_state_dict(params, strict=True)
        actual = self.model.state_dict()
        if set(actual) != set(params) or not all(
            torch.equal(actual[key], params[key]) for key in params):
            raise ValueError('Loaded tensors differ from checkpoint')
        self.state_key_count = len(params)
        self.model = self.model.to(device).eval().requires_grad_(False)
        if not all(p.is_cuda and not p.requires_grad for p in self.model.parameters()):
            raise ValueError('Codec must be frozen on GPU')
        torch.cuda.synchronize()
        self.load_seconds = time.perf_counter() - start

    def metadata(self):
        return {
            'name': 'TIC-SFMA', 'condition': 'adapt_icmh', 'quality': self.quality,
            'upstream_url': 'https://github.com/qingshi9974/ECCV2024-AdpatICMH',
            'upstream_commit': COMMIT, 'upstream_path': str(self.repository),
            'checkpoint_path': str(self.checkpoint), 'checkpoint_sha256': self.sha256,
            'checkpoint_size_bytes': self.checkpoint.stat().st_size,
            'weights_only': True, 'strict_load': True,
            'all_loaded_tensors_equal': True, 'state_key_count': self.state_key_count,
            'model_load_seconds': self.load_seconds,
            'device': self.device, 'rate_kind': 'estimated_likelihood_bits',
            'transmitted_components': ['y', 'z'],
            'shared_information': 'Pretrained codec and adapter weights shared; not charged per image; container overhead not measured',
            'mapper': 'D2 uint8 test mapper: short800/max1333, BGR -> float /255 -> RGB',
            'codec_alignment': 'zero_pad_right_bottom_to_multiple256',
            'reconstruction': 'crop_to_mapper_hw_then_clamp_0_1_float32',
            'source_sha256': {str(p.relative_to(self.repository)): common.sha256(p)
                              for p in sorted(self.repository.rglob('*.py'))},
            'license_review': 'No root LICENSE at pinned commit; borrowed BSD-style notices retained; no blanket weights license inferred',
            'paper_reproduction': False,
            'checkpoint_note': 'Published retrained TIC-SFMA weights; not paper Figure4 checkpoints',
        }

    def reconstruct(self, image, output_dir):
        from detectron2.data.detection_utils import read_image
        from detectron2.data import transforms
        if common.sha256(image['path']) != image['sha256']:
            raise ValueError('Source image changed since manifest')
        torch.cuda.synchronize()
        start = time.perf_counter()
        bgr = read_image(image['path'], format='BGR')
        if tuple(bgr.shape[:2]) != tuple(image['original_hw']):
            raise ValueError('Source dimensions changed')
        resize = transforms.ResizeShortestEdge([800, 800], 1333)
        mapped = resize.get_transform(bgr).apply_image(bgr)
        batch = torch.as_tensor(np.ascontiguousarray(mapped.transpose(2, 0, 1)))
        batch = batch.float().div(255).flip(0).unsqueeze(0).to(self.device)
        align = self.alignment_class(divisor=256, mode='pad', padding_mode='constant')
        padded = align.align(batch)
        independent = torch.nn.functional.pad(
            batch, (0, (-batch.shape[-1]) % 256, 0, (-batch.shape[-2]) % 256),
            mode='constant', value=0)
        if not torch.equal(padded, independent):
            raise ValueError('Official zero padding differs from expected protocol')
        torch.cuda.synchronize()
        mapper_seconds = time.perf_counter() - start
        start = time.perf_counter()
        with torch.inference_mode():
            output = self.model(padded)
            if output['x_hat'].shape != padded.shape or not bool(torch.isfinite(output['x_hat']).all()):
                raise ValueError('Invalid codec reconstruction')
            rgb = align.resume(output['x_hat']).clamp(0, 1)
            independent = output['x_hat'][..., :batch.shape[-2], :batch.shape[-1]].clamp(0, 1)
            if not torch.equal(rgb, independent):
                raise ValueError('Official crop differs from expected protocol')
            if set(output['likelihoods']) != {'y', 'z'}:
                raise ValueError('Expected y/z likelihoods')
            bits = {}
            for key, value in output['likelihoods'].items():
                if (not value.is_cuda or not bool(torch.isfinite(value).all())
                        or not bool((value > 0).all()) or not bool((value <= 1).all())):
                    raise ValueError('Invalid likelihoods')
                bits[key] = float((-torch.log2(value).sum()).item())
            bits['total'] = math.fsum(bits.values())
            # Same y/z rate contract, while retaining the distinct method label.
            common.validate_bits(bits, 'transtic')
            if not rgb.is_cuda or not bool(torch.isfinite(rgb).all()):
                raise ValueError('GPU/finite reconstruction check failed')
            torch.cuda.synchronize()
            codec_seconds = time.perf_counter() - start
            original = image['original_hw']
            if tuple(rgb.shape[-2:]) != shared_detector.expected_resized_shape(*original):
                raise ValueError('Detector input would have wrong size')
            path = Path(output_dir) / f"reconstruction_{image['image_id']}_float32.npy"
            with path.open('xb') as stream:
                np.save(stream, rgb[0].detach().cpu().numpy(), allow_pickle=False)
            row = {
                'image_id': image['image_id'], 'condition': 'adapt_icmh',
                'quality': self.quality, 'status': 'ok',
                'rate_definition': 'entropy_estimated', 'source_sha256': image['sha256'],
                'original_hw': list(original), 'detector_hw': list(batch.shape[-2:]),
                'codec_hw': list(padded.shape[-2:]), 'estimated_bits': bits,
                'reconstruction_precision': 'float32_no_png_quantization',
                'reconstruction_path': str(path), 'reconstruction_sha256': common.sha256(path),
                'gpu_synchronized': True, 'finite': True,
                'timing_seconds': {'mapper_and_padding': mapper_seconds, 'codec': codec_seconds},
            }
            return rgb[0], row


def aggregate(manifest, rows):
    if any(row.get('condition') != 'adapt_icmh' for row in rows):
        raise ValueError('Adapt-ICMH rows required')
    if len({row['quality'] for row in rows}) != 1:
        raise ValueError('Mixed qualities')
    # Reuse strict dimensional, ID, finite-bit, completeness and rate checks.
    summary = common.aggregate(manifest, rows, 'transtic')
    summary['condition'] = 'adapt_icmh'
    summary['quality'] = rows[0]['quality']
    return summary
