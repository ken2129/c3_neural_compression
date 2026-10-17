"""Finite container transport for the offline intake/claim/result fixture."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

# Fixed, image-owned code path, never Issue-controlled or worker-writable.
sys.path.insert(0, '/opt/fixture')
from probe import CONTROL_UID, WORKER_UID, identity_ok, status
from research_harness.intake_flow_fixture import FixtureSource, dummy_result, run_fixture
from research_harness.issue_intake import Intake
from research_harness.schema import ContractError, loads
from research_harness.store import SingleInstance

SCRIPT = '/opt/fixture/flow.py'
LIMIT = 4096


def read_message(stream):
    line = stream.readline(LIMIT + 1)
    if not line or len(line) > LIMIT or not line.endswith('\n'):
        raise ContractError('missing or oversized fixture message')
    return loads(line)


def manager():
    signal.alarm(15)
    if not identity_ok(status(), CONTROL_UID):
        raise ContractError('manager identity mismatch')
    os.umask(0o077)
    path = '/control/offline-fixture.sqlite'
    def exchange(job):
        print(json.dumps({'job': job}), flush=True)
        return read_message(sys.stdin)
    with SingleInstance(path + '.lock'):
        intake = Intake(path)
        try:
            first = run_fixture(intake, FixtureSource(), exchange)
        finally:
            intake.close()
        # Reopen the persistent ledger, as after a manager restart. Never redispatch.
        intake = Intake(path)
        try:
            def forbidden(job):
                raise ContractError('duplicate dispatch attempted')
            second = run_fixture(intake, FixtureSource(), forbidden)
            row = intake.db.execute('SELECT phase,result FROM offline_dummy_runs').fetchone()
            if row is None or row[0] != 'COMPLETED' or row[1] is None:
                raise ContractError('saved result missing')
        finally:
            intake.close()
    print(json.dumps({'fixture_only': True, 'execution_allowed': False,
                      'first': first, 'after_reopen': second, 'result_saved': True}), flush=True)


def worker():
    signal.alarm(8)
    if not identity_ok(status(), WORKER_UID):
        raise ContractError('worker identity mismatch')
    result = dummy_result(read_message(sys.stdin))
    Path('/work/dummy-result.json').write_text(json.dumps(result))
    print(json.dumps(result), flush=True)


def supervise():
    if not Path('/.dockerenv').exists() or os.getuid() != 0:
        raise ContractError('dedicated diagnostic container required')
    signal.alarm(25)
    control = subprocess.Popen([sys.executable, '-I', '-B', SCRIPT, 'manager'],
        user=CONTROL_UID, group=CONTROL_UID, extra_groups=[], env={'LANG': 'C.UTF-8'},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, close_fds=True)
    try:
        message = read_message(control.stdout)
        if type(message) is not dict or set(message) != {'job'}:
            raise ContractError('manager did not supply fixed job')
        worker = subprocess.run([sys.executable, '-I', '-B', SCRIPT, 'worker'],
            input=json.dumps(message['job'])+'\n', capture_output=True, text=True,
            user=WORKER_UID, group=WORKER_UID, extra_groups=[], env={'LANG': 'C.UTF-8'},
            close_fds=True, timeout=10, check=True)
        if len(worker.stdout) > LIMIT:
            raise ContractError('oversized worker output')
        control.stdin.write(worker.stdout); control.stdin.flush(); control.stdin.close()
        summary = read_message(control.stdout)
        control.wait(timeout=3)
        if control.returncode:
            raise ContractError('manager rejected output')
        print(json.dumps(summary), flush=True)
    finally:
        if not control.stdin.closed:
            control.stdin.close()


if __name__ == '__main__':
    try:
        if sys.argv[1:] == []:
            supervise()
        elif sys.argv[1:] == ['manager']:
            manager()
        elif sys.argv[1:] == ['worker']:
            worker()
        else:
            raise ContractError('unsupported fixture mode')
    except Exception as exc:
        print(json.dumps({'fixture_only': True, 'execution_allowed': False,
                          'failed': type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
