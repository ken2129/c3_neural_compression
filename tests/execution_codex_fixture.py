#!/usr/local/bin/python
"""Model-free app-server fixture. It actually edits and runs a CPU experiment."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if '--version' in sys.argv:
    print('codex-cli 0.160.0');raise SystemExit()

def send(value):print(json.dumps(value),flush=True)
def receive():return json.loads(sys.stdin.readline())
def event(method,**params):send(dict(method=method,params=dict(threadId='native-thread',turnId='native-turn',**params)))

initialize=receive()
send(dict(id=initialize['id'],result=dict(codexHome=os.environ['CODEX_HOME'],platformOs='linux',platformFamily='unix')))
receive();thread=receive();cwd=thread['params']['cwd'];model=thread['params']['model']
send(dict(id=thread['id'],result=dict(thread=dict(id='native-thread',ephemeral=True,cliVersion='0.160.0',cwd=cwd,turns=[]),
    cwd=cwd,model=model,approvalPolicy='never',approvalsReviewer='user',instructionSources=[],sandbox=dict(type='readOnly'))))
turn=receive();send(dict(id=turn['id'],result=dict(turn=dict(id='native-turn',status='inProgress',error=None))))
event('turn/started',turn=dict(id='native-turn',status='inProgress',error=None))
plan=json.loads(turn['params']['input'][0]['text'].split('Approved plan:\n',1)[1])
output=Path('output')/('issue-'+str(plan['direct_pr']['issue_number']));output.mkdir(parents=True,exist_ok=True)
try:Path(os.environ['C3_TEST_PRIVATE']).read_text();denied=False
except PermissionError:denied=True
evidence=dict(uid=os.getuid(),cwd=str(Path.cwd()),pid=os.getpid(),private_access_denied=denied,
              initialize_count=1,thread_id='native-thread',turn_id='native-turn')
(output/'native-session.json').write_text(json.dumps(evidence))
script=output/'experiment.py'
items=[]
def action(kind,number,callback):
    begin=dict(id=str(number),type=kind);event('item/started',item=begin)
    code=callback()
    done=dict(begin,status='completed',exitCode=code,command='ordinary native experiment command')
    event('item/completed',item=done);items.append(done)

action('fileChange',1,lambda:script.write_text('raise ValueError("controlled CPU fixture failure")\n') and None)
action('commandExecution',2,lambda:subprocess.run([sys.executable,str(script)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode)
mode=os.environ.get('C3_TEST_MODE','success')
if mode=='refresh':
    auth_path=Path(os.environ['CODEX_HOME'])/'auth.json'
    auth=json.loads(auth_path.read_text());auth['tokens']['access_token']='OFFLINE-REFRESHED-ACCESS'
    auth_path.write_text(json.dumps(auth))
if mode=='hang':
    script.write_text('import os,sys,time,subprocess\nfrom pathlib import Path\n'
        'p=subprocess.Popen([sys.executable,"-c","import os,time;os.setsid();time.sleep(60)"])\n'
        f'Path({str(output/"escaped.pid")!r}).write_text(str(p.pid))\n'
        'while True:time.sleep(1)\n')
else:
    action('fileChange',3,lambda:script.write_text('import time,json,os\nfrom pathlib import Path\n'
        f'Path({str(output/"running.pid")!r}).write_text(str(os.getpid()))\n'
        'time.sleep(.8)\n'+f'Path({str(output/"metrics.json")!r}).write_text(json.dumps({{"value":7}}))\n') and None)
action('commandExecution',4,lambda:subprocess.run([sys.executable,str(script)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode)
result=dict(outcome='completed',summary='CPU fixture only: failed, repaired and awaited one command.',
    pr_url='https://github.com/ken2129/c3_neural_compression/pull/777')
final=dict(id='final',type='agentMessage',phase='final_answer',text=json.dumps(result))
event('item/started',item=dict(id='final',type='agentMessage',phase='final_answer'))
event('item/completed',item=final)
event('turn/completed',turn=dict(id='native-turn',status='completed',error=None,itemsView='summary',items=[final]))
