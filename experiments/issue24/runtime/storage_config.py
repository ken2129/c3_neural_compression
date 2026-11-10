"""Issue24 configuration selected from a fixed private job."""
import json,os
from pathlib import Path
from pilot_config import get_config as pilot_config

def get_config(variant):
 job=json.loads(Path(os.environ['C3_ISSUE49_JOB']).read_text())
 if variant!=job['variant']:raise ValueError('Variant mismatch')
 config=pilot_config(job['name'])
 if config.experiment_kwargs.config.output_dir!=job['output_dir']:raise ValueError('Output mismatch')
 return config
