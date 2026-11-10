"""Run the six fixed pilot jobs once, within one shared deadline."""
from pathlib import Path
import os,json,time,hashlib,fcntl,importlib.metadata as metadata
from supervise import run

def main():
 root=Path(__file__).resolve().parents[1];pilot=root/'pilot'
 plan=json.loads((pilot/'execution_plan.json').read_text())
 if not set(plan['CPU_affinity']).issubset(os.sched_getaffinity(0)):raise ValueError('CPU assignment changed')
 os.sched_setaffinity(0,plan['CPU_affinity'])
 approval=json.loads((pilot/'approval.json').read_text())
 if approval.get('approved') is not True or approval.get('plan_sha256')!=hashlib.sha256((pilot/'execution_plan.json').read_bytes()).hexdigest():
  raise ValueError('This exact GPU pilot scope has not been approved')
 expected=json.loads((root/'cpu_preflight.json').read_text())['versions']
 for package,version in expected.items():
  if metadata.version(package)!=version:raise ValueError('Dependency version changed: '+package)
 if metadata.version('nvidia-cuda-nvcc-cu12')!='12.6.85':raise ValueError('CUDA compiler version changed')
 with (root/'control/sequence.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  prior=root/'control/smoke_image_receipt.json'
  if sorted(x.name for x in (root/'control').glob('*_receipt.json'))!=['smoke_image_receipt.json']:raise ValueError('Unexpected prior attempts; no retry')
  if hashlib.sha256(prior.read_bytes()).hexdigest()!=plan['retained_failed_receipt_sha256']:raise ValueError('Failure receipt changed')
  remaining=7200-(time.time()-plan['original_start_unix'])
  if remaining<=0:raise RuntimeError('Original wall limit exhausted')
  os.environ['C3_PILOT_DEADLINE']=str(time.monotonic()+remaining)
  for name in plan['remaining_job_ids']:
   if time.monotonic()>=float(os.environ['C3_PILOT_DEADLINE']):raise RuntimeError('Overall deadline reached')
   run(pilot/'jobs'/f'{name}.json')
if __name__=='__main__':main()
