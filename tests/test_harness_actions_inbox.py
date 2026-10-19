"""Real SQLite + Unix IPC, no registration, credentials, network or models."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research_harness.actions_inbox import (Inbox, IntakeBlocked, ReceiptServer,
    validate_receipt, validate_policy, submit_main, _line, MAX_RECEIPT)
from research_harness.schema import ContractError, canonical


def policy():
    return dict(schema='c3-resident-intake-v1', enabled=True, workflow_shas=['a'*40])


def receipt(run=1, issue=92, attempt=1):
    return dict(schema='c3-issue-event-v1', repository='ken2129/c3_neural_compression',
        repository_id=1312915009, issue_number=issue, event_name='issue_comment', action='created',
        actor_id=55587773, comment_id=123456, run_id=run, run_attempt=attempt,
        workflow_sha='a'*40, receipt_id=f'actions:1312915009:{run}')


class ActionsInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = Inbox(self.root/'inbox.sqlite')
        self.ipc = self.root/'ipc'; self.ipc.mkdir(mode=0o711)
        self.server = None

    def tearDown(self):
        if self.server: self.server.close()
        self.tmp.cleanup()

    def start(self, *, uid=None, config=None):
        self.server = ReceiptServer(self.ipc/'events.sock', self.inbox,
            policy() if config is None else config, runner_uid=os.getuid() if uid is None else uid)
        self.server.start()

    def send(self, value, *, raw=None, lose_ack=False):
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(3); peer.connect(str(self.ipc/'events.sock'))
            peer.sendall(canonical(value)+b'\n' if raw is None else raw)
            peer.shutdown(socket.SHUT_WR)
            if lose_ack: return
            return json.loads(peer.makefile('rb').readline())

    def test_real_commit_is_visible_in_new_connection_before_ack(self):
        self.start(); answer = self.send(receipt())
        self.assertEqual(answer['status'], 'enqueued')
        with sqlite3.connect(self.inbox.path) as db:
            self.assertEqual(db.execute('SELECT receipt,phase FROM events').fetchall(), [(receipt()['receipt_id'],'PENDING')])
        self.assertEqual(self.inbox.path.stat().st_mode & 0o777, 0o600)

    def test_same_actions_run_attempts_are_duplicate_not_another_execution(self):
        self.start(); self.assertEqual(self.send(receipt())['status'], 'enqueued')
        self.assertEqual(self.send(receipt(attempt=3))['status'], 'duplicate')
        self.assertEqual(self.inbox.take()['issue_number'], 92)
        self.assertIsNone(self.inbox.take())
        with sqlite3.connect(self.inbox.path) as db:
            self.assertEqual(db.execute('SELECT last_attempt FROM events').fetchone(), (3,))

    def test_receipt_conflict_is_rejected_without_replacing_prior(self):
        self.start(); self.send(receipt())
        self.assertIn('error', self.send(receipt(issue=93)))
        self.assertEqual(self.inbox.take()['issue_number'], 92)

    def test_lost_ack_is_reconciled_after_real_durable_commit(self):
        self.start(); self.send(receipt(), lose_ack=True)
        self.assertEqual(self.send(receipt(attempt=2))['status'], 'duplicate')
        self.assertEqual(self.inbox.take()['run_attempt'], 1)

    def test_later_approval_event_survives_prior_unapproved_outcome(self):
        self.start(); self.send(receipt()); first = self.inbox.take()
        self.inbox.finish(first['receipt_id'], dict(issue=92,phase='NOT_APPROVED',dispatched=False))
        self.assertEqual(self.send(receipt(run=2))['status'], 'enqueued')
        self.assertEqual(self.inbox.take()['run_id'], 2)

    def test_real_queue_restart_recovers_delivery_lease_not_claim(self):
        self.inbox.enqueue(receipt()); self.inbox.take()
        reopened = Inbox(self.inbox.path); reopened.recover()
        self.assertEqual(reopened.take(), receipt())

    def test_finished_delivery_does_not_recover_or_reenqueue(self):
        self.inbox.enqueue(receipt()); self.inbox.take()
        self.inbox.finish(receipt()['receipt_id'],dict(issue=92,phase='WORKER_FINISHED',dispatched=True))
        self.inbox.recover(); self.assertIsNone(self.inbox.take())
        self.assertEqual(self.inbox.enqueue(receipt(attempt=2))['status'], 'duplicate')

    def test_second_event_is_durable_while_first_is_active(self):
        self.start(); self.send(receipt()); self.inbox.take()
        self.assertEqual(self.send(receipt(run=2,issue=93))['status'], 'enqueued')
        with self.inbox.connect() as db:
            self.assertEqual(db.execute('SELECT phase FROM events ORDER BY rowid').fetchall(), [('ACTIVE',),('PENDING',)])

    def test_off_and_cleanup_latch_reject_delivery(self):
        config=policy(); config['enabled']=False; self.start(config=config)
        self.assertIn('error',self.send(receipt())); self.assertIsNone(self.inbox.take())
        self.inbox.block()
        with self.assertRaises(IntakeBlocked):self.inbox.enqueue(receipt())
        with self.assertRaises(IntakeBlocked):self.inbox.take()

    def test_real_kernel_peer_uid_not_owner_fields_authenticates_ipc(self):
        self.start(uid=os.getuid()+1)
        self.assertIn('error',self.send(receipt()))
        self.assertIsNone(self.inbox.take())

    def test_cleanup_latch_still_reconciles_known_commit_without_new_work(self):
        self.inbox.enqueue(receipt());self.inbox.block()
        self.assertEqual(self.inbox.enqueue(receipt(attempt=2))['status'],'duplicate')
        with self.assertRaises(IntakeBlocked):self.inbox.enqueue(receipt(run=2))
        with self.assertRaises(IntakeBlocked):self.inbox.take()

    def test_db_failure_never_returns_success_ack(self):
        self.start()
        with patch.object(self.inbox,'enqueue',side_effect=sqlite3.OperationalError('private failure')):
            response=self.send(receipt())
        self.assertEqual(response,{'error':'RECEIPT_REJECTED'})
        self.assertIsNone(self.inbox.take())

    def test_exact_single_line_and_no_shell_or_body_fields(self):
        self.start()
        for raw in [b'{"schema":"x"}\n',canonical(receipt())+b'\n{}\n',b'x'*8193+b'\n']:
            with self.subTest(size=len(raw)):self.assertIn('error',self.send(None,raw=raw))
        value=receipt();value['body']='$(arbitrary-command)'
        self.assertIn('error',self.send(value));self.assertIsNone(self.inbox.take())

    def test_bad_identity_ids_events_workflow_and_bool_numbers_rejected(self):
        for key,value in [('repository','other/repo'),('repository_id',True),('actor_id',1),
                          ('issue_number',True),('issue_number',0),('run_id',2**63),
                          ('event_name','pull_request'),('action','synchronize'),
                          ('workflow_sha','b'*40),('comment_id',None)]:
            v=receipt();v[key]=value
            with self.subTest(key=key),self.assertRaises(ContractError):validate_receipt(v,policy())

    def test_issues_event_has_no_comment_id(self):
        v=receipt();v.update(event_name='issues',action='opened',comment_id=None)
        self.assertEqual(validate_receipt(v,policy()),v)

    def test_duplicate_json_keys_and_oversized_wire_rejected(self):
        for raw in [b'{"x":1,"x":2}\n',b'{}',b' '*8193+b'\n']:
            with self.assertRaises(ContractError):_line(io.BytesIO(raw),MAX_RECEIPT)

    def test_submit_cli_uses_real_socket_and_exact_contract(self):
        self.start(); output=io.BytesIO()
        # These substitutions only map fixture UIDs/path. Linux SO_PEERCRED and
        # SQLite commit/ACK remain real. UID10003 isolation needs Docker tests.
        with patch('research_harness.actions_inbox.RUNNER_UID',os.getuid()), \
             patch('research_harness.actions_inbox.MANAGER_UID',os.getuid()), \
             patch('research_harness.actions_inbox.SOCKET_PATH',str(self.server.path)), \
             patch('sys.stdin',SimpleNamespace(buffer=io.BytesIO(canonical(receipt())+b'\n'))), \
             patch('sys.stdout',SimpleNamespace(buffer=output)):
            self.assertEqual(submit_main(['--receipt-stdin']),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'enqueued')

    def test_no_model_or_issue_scan_is_imported_by_ingress(self):
        import research_harness.actions_inbox as module
        source=Path(module.__file__).read_text()
        for token in ('subprocess.', 'list_candidates(', 'get_issue(', 'Controller('):
            self.assertNotIn(token,source)
