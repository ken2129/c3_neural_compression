"""Modeled Docker/worker endpoint for real manager pipe protocol tests."""
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_harness.schema import canonical
from research_harness.direct_pr_service import DirectPRResult

def send(value):sys.stdout.buffer.write(canonical(value)+b'\n');sys.stdout.buffer.flush()
def receive():return json.loads(sys.stdin.buffer.readline())

if __name__=='__main__':
    mode=sys.argv[1];marker=Path(sys.argv[2]);prepare=receive();policy=prepare['policy'];plan=prepare['plan']
    epoch=dict(schema_version=1,container_id=policy['container_id'],boot_id='12345678-1234-1234-1234-123456789abc',
        pid_namespace_inode=77,pid1_start_ticks=42)
    if mode=='wrong_identity':epoch['container_id']='f'*64
    send(dict(op='ready',epoch=epoch,policy_sha256=hashlib.sha256(canonical(policy)).hexdigest()))
    frame=receive();binding=frame['binding'];marker.write_text('EXECUTE_RECEIVED')
    if mode=='truncate':sys.stdout.buffer.write(b'{');sys.stdout.buffer.flush();raise SystemExit()
    if mode=='cancel':
        send(dict(op='status',phase='codex_running'))
        while receive()['op']!='cancel':pass
        send(dict(op='worker_failure',binding=binding,cleanup_verified=True,reason='EXECUTION_FAILED'))
    else:
        result=DirectPRResult(binding,plan['model'],'completed','Fixture report only.',
            'https://github.com/ken2129/c3_neural_compression/pull/777',.5)
        send(dict(op='worker_result',binding=binding,cleanup_verified=mode!='no_cleanup',result=result.to_dict()))
