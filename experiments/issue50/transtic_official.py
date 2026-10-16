"""Pinned official TransTIC resize alignment; shared detector never resizes."""
import importlib.util
from pathlib import Path
import time

import numpy as np
import torch
from c3_neural_compression.evaluation import comparison as common, transtic_adapter


class OfficialTransTIC:
    def __init__(self, repository, checkpoint, sha256, quality):
        self.adapter=transtic_adapter.TransTICAdapter(repository,checkpoint,quality,'cuda:0')
        assert common.sha256(checkpoint)==sha256
        self.adapter.load()
        spec=importlib.util.spec_from_file_location('transtic_official_alignment',Path(repository)/'examples/utils/alignment.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.Alignment=module.Alignment
        self.quality=quality

    def metadata(self):
        result=self.adapter.metadata()
        result['alignment_approved']='2026-10-16 user selected official resize; no padding alternative selection after AP'
        return result

    def reconstruct(self,image,output_dir):
        from detectron2.data.detection_utils import read_image
        assert common.sha256(image['path'])==image['sha256']
        mapped,dims=transtic_adapter.mapper_bgr(read_image(image['path'],format='BGR'))
        batch=torch.as_tensor(np.ascontiguousarray(mapped.transpose(2,0,1))).float().div(255).flip(0).unsqueeze(0).cuda()
        align=self.Alignment(divisor=256,mode='resize')
        aligned=align.align(batch)
        independent=torch.nn.functional.interpolate(batch,size=dims.codec_hw,mode='bilinear',align_corners=False) if dims.codec_hw!=dims.detector_hw else batch
        assert torch.equal(aligned,independent)
        torch.cuda.synchronize();start=time.perf_counter()
        with torch.inference_mode():
            output=self.adapter._model(aligned)
            assert set(output['likelihoods'])=={'y','z'}
            assert output['x_hat'].shape==aligned.shape and bool(torch.isfinite(output['x_hat']).all())
            rgb=align.resume(output['x_hat']).clamp(0,1)
            check=torch.nn.functional.interpolate(output['x_hat'],size=dims.detector_hw,mode='bilinear',align_corners=False) if dims.codec_hw!=dims.detector_hw else output['x_hat']
            assert torch.equal(rgb,check.clamp(0,1))
            bits={}
            for name,value in output['likelihoods'].items():
                assert bool(torch.isfinite(value).all()) and bool((value>0).all()) and bool((value<=1).all())
                bits[name]=float((torch.log(value).sum()/-__import__('math').log(2)).item())
            bits['total']=bits['y']+bits['z']
        torch.cuda.synchronize()
        path=Path(output_dir)/f"reconstruction_{image['image_id']}_float32.npy"
        with path.open('xb') as f:np.save(f,rgb[0].cpu().numpy(),allow_pickle=False)
        row={'image_id':image['image_id'],'condition':'transtic','quality':self.quality,'status':'ok','original_hw':list(dims.original_hw),'detector_hw':list(dims.detector_hw),'codec_hw':list(dims.codec_hw),'estimated_bits':bits,'reconstruction_path':str(path),'reconstruction_sha256':common.sha256(path),'source_sha256':image['sha256'],'alignment':'official resize bilinear align_corners=False before/after codec','alignment_equal_independent':True,'rate_definition':'entropy_estimated','timing_seconds':{'codec':time.perf_counter()-start}}
        return rgb[0],row
