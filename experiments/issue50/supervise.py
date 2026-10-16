"""Bounded single GPU subprocess accounting includes interpreter startup and exit."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from codec import write


def main(root,stage,method):
    root=Path(root).resolve();private=root/'var/issue50_evaluation_20261016'
    plan=json.loads((private/'preflight.json').read_text())
    receipts=private/'gpu_receipts';receipts.mkdir(exist_ok=True)
    receipt=receipts/f'{method}_{stage}.json'
    assert not receipt.exists(),'No implicit retries'
    used=sum(json.loads(p.read_text())['gpu_seconds'] for p in receipts.glob('*.json'))
    assert used<7000
    budget=min(2100,7100-used)
    assert not subprocess.check_output(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader'],text=True).strip(),'GPU compute job active'
    env=os.environ.copy();env.update(CUDA_VISIBLE_DEVICES='0',JAX_PLATFORMS='cpu',PYTHONDONTWRITEBYTECODE='1')
    py=root/('var/issue20_resume_20261010_retry1/eval-env/bin/python' if stage=='evaluate' or method=='transtic' else 'var/issue23_smoke_20261010/eval-env/bin/python')
    script=Path(__file__).with_name(stage+'.py')
    start=time.monotonic();state={'status':'running','stage':stage,'method':method,'gpu_seconds':0,'started_unix':time.time(),'pid':None,'allocated_seconds':budget}
    handoff=root/'var/issue50_codex_child_20261016/handoff_status.json'
    with (private/f'{method}_{stage}.log').open('x') as log:
        proc=subprocess.Popen([str(py),str(script),'--root',str(root),'--method',method],env=env,stdout=log,stderr=subprocess.STDOUT)
        state['pid']=proc.pid;write(receipt,state)
        try:
            while proc.poll() is None:
                state['gpu_seconds']=time.monotonic()-start;write(receipt,state)
                h=json.loads(handoff.read_text());h.update(stage=method+' '+stage,status='running',gpu_pid=proc.pid,gpu_seconds=used+state['gpu_seconds']);write(handoff,h)
                if state['gpu_seconds']>budget:
                    proc.terminate();proc.wait(timeout=20);raise RuntimeError('Cumulative GPU budget guard')
                time.sleep(2)
            state.update(status='complete' if proc.returncode==0 else 'failed',exit_code=proc.returncode)
        except BaseException as e:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=20)
            state.update(status='failed',error=str(e));raise
        finally:
            state['gpu_seconds']=time.monotonic()-start;write(receipt,state)
            h=json.loads(handoff.read_text());h.update(status=state['status'],stage=method+' '+stage,gpu_pid=None,gpu_seconds=used+state['gpu_seconds']);write(handoff,h)
    assert state['status']=='complete'


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--stage',choices=['codec','evaluate'],required=True);p.add_argument('--method',required=True);a=p.parse_args();main(a.root,a.stage,a.method)
