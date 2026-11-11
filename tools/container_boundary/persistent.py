"""One dedicated-container persistent diagnostic invocation; no live execution."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
sys.path.insert(0, '/opt/fixture')
from flow import read_message, LIMIT
from probe import CONTROL_UID, WORKER_UID, identity_ok, status, denied, attempt_open
from research_harness.intake_flow_fixture import dummy_result
from research_harness.persistent_fixture import initialize, run_persistent
from research_harness.schema import ContractError
from research_harness.store import SingleInstance

SCRIPT='/opt/fixture/persistent.py'


def manager(mode):
    signal.alarm(15)
    if not identity_ok(status(),CONTROL_UID):raise ContractError('manager identity mismatch')
    os.umask(0o077)
    with SingleInstance('/control/manager.lock'):
        if mode=='initialize':
            result=initialize('/control')
        else:
            def exchange(job):
                print(json.dumps({'job':job}),flush=True)
                return read_message(sys.stdin)
            result=run_persistent('/control',exchange)
    print(json.dumps({'summary':result}),flush=True)


def worker():
    signal.alarm(8)
    if not identity_ok(status(),WORKER_UID):raise ContractError('worker identity mismatch')
    for name in ('state.sqlite','initialized.json','dispatch.json'):
        for flags in (os.O_RDONLY,os.O_WRONLY):
            if not denied(lambda n=name,f=flags:attempt_open('/control/'+n,f)):
                raise ContractError('worker can access manager state or target missing')
    print(json.dumps(dummy_result(read_message(sys.stdin))),flush=True)


def supervise(mode):
    if not Path('/.dockerenv').exists() or os.getuid()!=0:raise ContractError('dedicated container required')
    signal.alarm(25)
    control=subprocess.Popen([sys.executable,'-I','-B',SCRIPT,'manager',mode],
        user=CONTROL_UID,group=CONTROL_UID,extra_groups=[],env={'LANG':'C.UTF-8'},
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,close_fds=True)
    try:
        message=read_message(control.stdout)
        if type(message) is dict and set(message)=={'job'}:
            result=subprocess.run([sys.executable,'-I','-B',SCRIPT,'worker'],
                input=json.dumps(message['job'])+'\n',capture_output=True,text=True,
                user=WORKER_UID,group=WORKER_UID,extra_groups=[],env={'LANG':'C.UTF-8'},
                close_fds=True,timeout=10,check=True)
            if len(result.stdout)>LIMIT:raise ContractError('oversized output')
            control.stdin.write(result.stdout);control.stdin.flush()
            message=read_message(control.stdout)
        control.stdin.close();control.wait(timeout=3)
        if control.returncode or type(message) is not dict or set(message)!={'summary'}:
            raise ContractError('manager failed')
        print(json.dumps(message['summary']),flush=True)
    finally:
        if not control.stdin.closed:control.stdin.close()


if __name__=='__main__':
    try:
        if sys.argv[1:] in (['initialize'],['run']):supervise(sys.argv[1])
        elif len(sys.argv)==3 and sys.argv[1]=='manager' and sys.argv[2] in ('initialize','run'):manager(sys.argv[2])
        elif sys.argv[1:]==['worker']:worker()
        else:raise ContractError('unsupported mode')
    except Exception as exc:
        print(json.dumps({'fixture_only':True,'execution_allowed':False,'failed':type(exc).__name__}),file=sys.stderr)
        sys.exit(1)
