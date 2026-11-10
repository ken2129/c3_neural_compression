"""Bounded one-job GPU supervisor. Does not grant scope or transfer approval."""
import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


def read(path): return json.loads(Path(path).read_text())
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def size(root): return sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file())
def write(path, value):
    p=Path(path);temporary=p.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n');temporary.replace(p)


def child_limits():
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[-2:])
    # If the supervisor disappears, terminate its direct experiment process.
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0:
        raise RuntimeError('Could not install parent-death signal')
    if os.getppid()==1:raise RuntimeError('Supervisor disappeared before child startup')

def rss_group(pgid):
    total=0
    for path in Path('/proc').iterdir():
        if not path.name.isdigit(): continue
        try:
            if os.getpgid(int(path.name)) != pgid: continue
            for line in (path/'status').read_text().splitlines():
                if line.startswith('VmRSS:'):total+=int(line.split()[1])*1024
        except (FileNotFoundError, ProcessLookupError, PermissionError):pass
    return total

def interrupt(signum, frame):
    raise KeyboardInterrupt('Supervisor signal '+str(signum))


def run(job_file):
    signal.signal(signal.SIGTERM, interrupt)
    job_file=Path(job_file).resolve();job=read(job_file)
    plan=read(job['scope_plan_file']);approval=read(job['scope_approval_file'])
    if approval.get('approved') is not True or approval.get('plan_sha256')!=digest(job['scope_plan_file']):
        raise ValueError('Approval does not cover exact plan')
    control=Path(plan['control_dir']).resolve();control.mkdir(exist_ok=True)
    receipt=control/(job['job_id']+'_receipt.json')
    if receipt.exists(): raise ValueError('Attempt already exists; never retry or replace evidence')
    gpu=job['GPU'];assert gpu in plan['GPU_indices']
    if job['job_id'] not in plan['permitted_job_ids']:
        raise ValueError('Job is not in scope plan')
    registry=read(plan['job_registry'])
    if registry.get('scope_plan_sha256')!=digest(job['scope_plan_file']):
        raise ValueError('Derived registry does not match scope plan')
    if digest(job_file)!=registry.get('job_sha256',{}).get(job['job_id']):
        raise ValueError('Job needs an exact config receipt derived within the approved scope')
    if digest(job['manifest'])!=job['manifest_sha256']:
        raise ValueError('Frozen manifest changed')
    if read(job['subset_ids_file'])!=read(job['manifest'])['image_ids']:
        raise ValueError('Subset order differs from frozen manifest')
    if job['mode']=='evaluate' and digest(job['evaluation_config'])!=job['evaluation_config_sha256']:
        raise ValueError('Evaluation configuration changed')
    for path,expected in plan.get('asset_sha256',{}).items():
        if digest(path)!=expected: raise ValueError('Teacher/evaluator asset mismatch')
    runtime=Path(plan['runtime_root']).resolve()
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=runtime,text=True).strip()!=plan['runtime_commit']:
        raise ValueError('Runtime commit mismatch')
    if subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=runtime,text=True).strip():
        raise ValueError('Runtime has modified tracked files')
    for path,expected in plan['execution_source_sha256'].items():
        if digest(path)!=expected: raise ValueError('Execution wrapper hash mismatch')
    output=Path(job['job_dir']).resolve();base=Path(plan['private_root']).resolve()
    if not output.is_relative_to(base): raise ValueError('Output must be private Issue49 descendant')
    output.mkdir(exist_ok=False)
    # Exclusive per-GPU file prevents two local supervisors racing the GPU check.
    lock=control/f'GPU{gpu}.lock';handle=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    os.write(handle,str(os.getpid()).encode());os.close(handle)
    start=time.monotonic();proc=None;reason=None
    record={'status':'preflight','supervisor_PID':os.getpid(),'GPU':gpu,'job_id':job['job_id'],
            'started_at':datetime.now(timezone.utc).isoformat(),'job_sha256':digest(job_file),
            'plan_sha256':digest(job['scope_plan_file'])}
    write(receipt,record)
    def prior_seconds():
        total=0
        for path in control.glob('*_receipt.json'):
            if path==receipt: continue
            item=read(path)
            if item.get('status')=='running':
                try: os.kill(item['supervisor_PID'],0)
                except ProcessLookupError: raise RuntimeError('Orphaned receipt requires reconciliation')
                total+=max(item.get('gpu_process_seconds',0),time.time()-item['started_unix'])
            else: total+=item.get('gpu_process_seconds',0)
        return total
    try:
        uuids=dict(line.split(', ') for line in subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid','--format=csv,noheader'],text=True).strip().splitlines())
        occupied=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader'],text=True).splitlines()
        if uuids[str(gpu)] != plan['GPU_UUIDs'][str(gpu)]:
            raise RuntimeError('Selected GPU UUID changed')
        stats=subprocess.check_output(['nvidia-smi','--id='+str(gpu),'--query-gpu=utilization.gpu,memory.used','--format=csv,noheader,nounits'],text=True,timeout=10).strip().split(',')
        if float(stats[0])>5 or float(stats[1])>256:
            raise RuntimeError('Selected GPU is not idle')
        if any(line.split(', ')[0]==uuids[str(gpu)] for line in occupied):
            raise RuntimeError('Selected GPU occupied; no interference')
        if shutil.disk_usage(base).free < plan['disk_reserve_bytes']+job['max_local_growth_bytes']:
            raise RuntimeError('Cannot preserve3GiB reserve through this job')
        if prior_seconds()+job['max_gpu_seconds']>plan['max_GPU_seconds']:
            raise RuntimeError('Predicted cumulative GPU ceiling exceeded')
        env=dict(os.environ,CUDA_VISIBLE_DEVICES=str(gpu),JAX_PLATFORMS='cuda,cpu',
                 XLA_PYTHON_CLIENT_PREALLOCATE='false',PYTHONPATH=str(runtime.parent),
                 PYTHONDONTWRITEBYTECODE='1',OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2',WANDB_MODE='disabled')
        env.update(plan['runtime_environment'])
        env['XLA_FLAGS']=(env.get('XLA_FLAGS','')+' --xla_gpu_exclude_nondeterministic_ops=true').strip()
        env.update(C3_COCO_ROOT=job['image_root'],C3_COCO_ANNOTATIONS=job['annotation_file'],
                   C3_SUBSET_IDS=job['subset_ids_file'],C3_OUTPUT_ROOT=str(output))
        log=(output/'process.log').open('x')
        proc=subprocess.Popen([plan['interpreter'],plan['runner'],str(job_file)],cwd=runtime,env=env,
                              stdout=log,stderr=subprocess.STDOUT,start_new_session=True,preexec_fn=child_limits)
        start=time.monotonic();record.update(status='running',PID=proc.pid,started_unix=time.time())
        write(receipt,record)
        while proc.poll() is None:
            elapsed=time.monotonic()-start;free=shutil.disk_usage(base).free
            record.update(gpu_process_seconds=elapsed,disk_free_bytes=free,job_bytes=sum(size(p) for p in [output,Path(job['output_dir']),Path(job['evidence_dir'])] if p.exists()))
            current=sum(size(p) for p in plan['runtime_storage_roots'])
            record['new_artifact_bytes']=max(0,current-plan['runtime_storage_baseline_bytes'])
            record['peak_group_RSS_bytes']=max(record.get('peak_group_RSS_bytes',0),rss_group(proc.pid))
            gpu_stats=subprocess.check_output(['nvidia-smi','--id='+str(gpu),'--query-gpu=memory.used','--format=csv,noheader,nounits'],text=True,timeout=10).strip()
            record['sampled_peak_GPU_memory_MiB']=max(record.get('sampled_peak_GPU_memory_MiB',0),float(gpu_stats))
            active=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader'],text=True,timeout=10).splitlines()
            selected=[line for line in active if line.split(', ')[0]==uuids[str(gpu)]]
            write(receipt,record)
            if free<=plan['disk_reserve_bytes']:reason='disk_reserve'
            elif record['peak_group_RSS_bytes']>plan['max_RAM_bytes']:reason='group_RAM_ceiling'
            elif record['new_artifact_bytes']>plan['max_new_artifact_bytes']:reason='total_storage_ceiling'
            elif len(selected)>1:reason='selected_GPU_multiple_compute_processes'
            elif time.monotonic()>float(os.environ.get('C3_PILOT_DEADLINE','inf')):reason='overall_wall_ceiling'
            elif record['job_bytes']>job['max_local_growth_bytes']:reason='job_storage_ceiling'
            elif elapsed>job['max_gpu_seconds']:reason='job_GPU_time_ceiling'
            elif elapsed+prior_seconds()>plan['max_GPU_seconds']:reason='cumulative_GPU_time_ceiling'
            if reason:
                os.killpg(proc.pid,signal.SIGTERM)
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
                break
            time.sleep(1)
        log.close();record.update(status='complete' if proc.returncode==0 and reason is None else 'stopped_or_failed',
                                  returncode=proc.returncode,stop_reason=reason,gpu_process_seconds=time.monotonic()-start)
    except BaseException as error:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        record.update(status='failed',error=str(error),gpu_process_seconds=time.monotonic()-start if proc else 0)
        raise
    finally:
        record['finished_at']=datetime.now(timezone.utc).isoformat();write(receipt,record);lock.unlink()
    if record['status']!='complete':raise RuntimeError('Job failed or stopped; no automatic retry')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('job');args=p.parse_args();run(args.job)
