"""Offline IPC peer with real owned descendants; never imported by an image.

No Docker, UID impersonation, auth, model or network. Uses the production
failure and cleanup handshake helpers over actual unbuffered OS pipes.
"""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_harness_resident import resident, common
from research_harness.direct_pr_service import DirectPRResult
from research_harness.resident_runtime import install_subreaper, reap_children, erase_tree_contents


def main():
    mode,root=sys.argv[1],Path(sys.argv[2])
    install_subreaper()
    incoming=sys.stdin.buffer.raw
    outgoing=sys.stdout.buffer
    descendant=leader=None

    def record(phase):
        with (root/'trace.jsonl').open('a') as stream:stream.write(json.dumps({'phase':phase})+'\n')

    def cleanup():
        nonlocal descendant
        if descendant is not None:
            # Only the newly created double-fork descendant, now adopted by
            # this subreaper. No host-wide process/UID enumeration or signals.
            parent=int(Path(f'/proc/{descendant}/stat').read_text().rsplit(')',1)[1].split()[1])
            if parent!=os.getpid():raise RuntimeError('fixture descendant not owned')
            fd=os.pidfd_open(descendant)
            try:
                # A pidfd alone pins identity but not parentage. Require the
                # pinned process to remain this fixture's waitable child too.
                os.waitid(os.P_PIDFD,fd,os.WEXITED|os.WNOHANG|os.WNOWAIT)
                signal.pidfd_send_signal(fd,signal.SIGKILL)
            finally:os.close(fd)
            descendant=None
        until=time.monotonic()+2
        while not reap_children():
            if time.monotonic()>=until:raise RuntimeError('fixture ECHILD deadline')
            time.sleep(.005)
        record('DESCENDANTS_ECHILD')
        erase_tree_contents(root/'work',switch_uid=lambda _:None,
            root_uid=os.getuid(),worker_uid=os.getuid())
        if list((root/'work').iterdir()):raise RuntimeError('fixture files remain')
        record('WORKSPACE_EMPTY')

    try:
        frame=common.read_frame(incoming)
        if frame.get('op')!='prepare_goal':raise RuntimeError('fixture prepare order')
        plan=frame['plan'];(root/'work').mkdir()
        (root/'work'/'dummy-auth').write_bytes(b'not-real-auth')
        common.write_frame(outgoing,{'prepared':True})
        if common.read_frame(incoming)!={'op':'preflight'}:raise RuntimeError('fixture preflight order')
        common.write_frame(outgoing,{'ready':True})
        frame=common.read_frame(incoming)
        if frame.get('op')!='goal':raise RuntimeError('fixture goal order')
        binding=frame['binding'];record('GOAL_RECEIVED')
        if mode=='cancel':
            code="""import os,signal,time
if os.fork():os._exit(0)
os.setsid()
signal.signal(signal.SIGTERM,signal.SIG_IGN)
print(os.getpid(),flush=True)
time.sleep(8)
"""
            leader=subprocess.Popen([sys.executable,'-I','-S','-c',code],stdout=subprocess.PIPE)
            descendant=int(leader.stdout.readline());leader.wait(timeout=2)
            if reap_children():raise RuntimeError('live fixture descendant missed')
            record('DESCENDANT_STARTED')
            common.write_frame(outgoing,{'op':'recheck'})
            denial=common.read_frame(incoming)
            if denial.get('approved') is not False:raise RuntimeError('fixture expected actual denial')
            record('APPROVAL_DENIED')
            detail={'reason':'GOAL_APPROVAL_RECHECK_FAILED','stage':'turn',
                    'approval_failure':{'reason':denial['reason'],'origin':denial['origin']}}
            class TracedOutput:
                def write(_,raw):
                    result=outgoing.write(raw)
                    if 'goal_failure' in json.loads(raw):record('GOAL_FAILURE_SENT')
                    return result
                def flush(_):outgoing.flush()
            resident.acknowledge_goal_failure(incoming,TracedOutput(),binding['issue_number'],detail,cleanup=cleanup)
            with sqlite3.connect((root.parent/'state.sqlite').as_uri()+'?mode=ro',uri=True) as db:
                db.execute('PRAGMA query_only=ON')
                if db.execute('SELECT phase FROM service_runs WHERE issue=?',(binding['issue_number'],)).fetchone()!=('BLOCKED',):
                    raise RuntimeError('fixture ACK preceded durable BLOCKED')
            record('FAILURE_ACK')
        else:
            common.write_frame(outgoing,{'op':'recheck'})
            if common.read_frame(incoming)!={'approved':True}:raise RuntimeError('fixture B approval rejected')
            record('APPROVAL_VALID')
            result=DirectPRResult(binding,plan['model'],'completed','offline fixture',None,.01)
            common.write_frame(outgoing,{'direct_pr_result':result.to_dict()})
        if common.read_frame(incoming)!={'op':'job_finished'}:raise RuntimeError('fixture finish order')
        record('JOB_FINISHED')
        resident.send_cleaned(outgoing,cleanup=cleanup);record('CLEANED_SENT')
    finally:
        if descendant is not None:
            try:cleanup()
            except ProcessLookupError:pass
        if leader is not None:
            leader.stdout.close();leader.wait()


if __name__=='__main__':main()
