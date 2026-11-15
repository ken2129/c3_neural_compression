#!/usr/local/bin/python -IB
"""Fixed local runner / app-server fixtures. No model or network library."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
sys.path.insert(0,'/opt/service')
from research_harness.resident_diagnostic_fixture import A,B,receipt
from research_harness.schema import canonical


def runner():
    if os.getresuid()!=(10003,)*3:raise RuntimeError('fixture runner UID')
    until=time.monotonic()+30
    while not Path('/control-ipc/events.sock').exists():
        if time.monotonic()>until:raise RuntimeError('fixture inbox deadline')
        time.sleep(.05)
    statuses=[]
    def submit(value):
        result=subprocess.run(['/opt/c3/bin/submit-issue-event','--receipt-stdin'],input=canonical(value)+b'\n',
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30,env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8'})
        if result.returncode:raise RuntimeError('fixture receipt rejected')
        ack=json.loads(result.stdout)
        if ack.get('receipt_id')!=value['receipt_id']:raise RuntimeError('fixture ACK mismatch')
        statuses.append(ack['status'])
    submit(receipt(A,920001));submit(receipt(B,920002))
    while not Path('/control-ipc/diagnostic-two-cleaned').exists():
        if time.monotonic()>until:raise RuntimeError('fixture cleanup deadline')
        time.sleep(.05)
    submit(receipt(A,920001,2));submit(receipt(A,920003));submit(receipt(B,920004))
    with Path('/runner/delivery-result.json').open('x') as out:json.dump(statuses,out)
    while True:time.sleep(1)


def app_server():
    if os.getresuid()!=(10002,)*3 or os.getgroups():raise RuntimeError('fixture worker UID')
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if any(int(status[k],16) for k in ('CapEff','CapPrm','CapInh','CapAmb')) or status['NoNewPrivs'].strip()!='1':
        raise RuntimeError('fixture worker capabilities')
    plan=json.loads(Path('/work/worker-home/approved-plan.json').read_bytes());number=plan['direct_pr']['issue_number']
    if number not in (A,B):raise RuntimeError('fixture number')
    thread,turn='diagnostic-thread','diagnostic-turn'
    def send(value):sys.stdout.buffer.write(canonical(value)+b'\n');sys.stdout.buffer.flush()
    def event(method,**value):send(dict(method=method,params=dict(threadId=thread,turnId=turn,**value)))
    for line in sys.stdin.buffer:
        request=json.loads(line);method=request.get('method')
        if method=='initialize':
            send(dict(id=request['id'],result=dict(codexHome='/work/codex-home',platformOs='linux',platformFamily='unix')))
        elif method=='initialized':continue
        elif method=='thread/start':
            send(dict(id=request['id'],result=dict(thread=dict(id=thread,ephemeral=True,cliVersion='0.160.0',cwd='/work/goal',turns=[]),
                cwd='/work/goal',model=plan['model'],approvalPolicy='never',approvalsReviewer='user',instructionSources=[],sandbox={'type':'readOnly'})))
        elif method=='turn/start':
            send(dict(id=request['id'],result={'turn':dict(id=turn,status='inProgress',error=None)}))
            event('turn/started',turn=dict(id=turn,status='inProgress',error=None))
            Path('/dev/shm/resident-diag-worker').write_bytes(b'DUMMY IPC')
            Path('/work/goal/output/result.json').write_text('{"fixture_only":true}\n')
            if number==A:
                # Deliberately remain alive outside the leader's session.
                child=os.fork()
                if child==0:
                    os.setsid()
                    if os.fork():os._exit(0)
                    signal.signal(signal.SIGTERM,signal.SIG_IGN)
                    time.sleep(30);os._exit(0)
                os.waitpid(child,0)
                while True:time.sleep(.2)
            final=dict(id='final',type='agentMessage',phase='final_answer',
                text=json.dumps(dict(outcome='completed',summary='Fixed model-free fixture.',pr_url=None)))
            event('item/started',item=dict(id='final',type='agentMessage',phase='final_answer'))
            event('item/completed',item=final)
            event('turn/completed',turn=dict(id=turn,status='completed',error=None,itemsView='summary',items=[copy.deepcopy(final)]))
        else:raise RuntimeError('fixture RPC unsupported')


if __name__=='__main__':
    try:
        if Path('/proc/self/attr/current').read_text().strip()!='docker-default (enforce)':
            raise RuntimeError('fixture AppArmor enforcement missing')
        if Path(sys.argv[0]).name=='Runner.Listener':runner()
        else:app_server()
    except BaseException:
        print('FIXTURE_PROCESS_BLOCKED',file=sys.stderr,flush=True)
        os._exit(125)
