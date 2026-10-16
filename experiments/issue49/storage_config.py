"""Typed frozen-base configuration with Issue49 output/checkpoint locations."""
import json
import os
from pathlib import Path
from c3_neural_compression.configs import coco2017_detector_input


def get_config(config_string):
    job = json.loads(Path(os.environ['C3_ISSUE49_JOB']).read_text())
    if config_string != job['variant']:
        raise ValueError('Configuration variant does not match job')
    config = coco2017_detector_input.get_config(config_string)
    config.unlock()
    exp = config.experiment_kwargs.config
    exp.output_dir = str(Path(job['output_dir']).resolve())
    exp.checkpointing.directory = str(Path(job['evidence_dir']).resolve()/'checkpoints')
    exp.checkpointing.save_every_steps = 50
    exp.checkpointing.resume = False
    if exp.opt.num_noise_steps != 500 or exp.opt.max_num_ste_steps != 50:
        raise ValueError('Issue49 requires 500 noise + maximum50 STE')
    config.lock()
    return config
