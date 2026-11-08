"""Real startup gate/SQLite/socket regressions; no model, auth or GitHub.

The manager UID/boot records are fixtures. Actual manager() ordering, formal
claim state, durable inbox and Unix IPC run at the test UID, not as a proof of
deployment isolation.
"""
import io
import json
import os
import socket
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_harness.actions_inbox import Inbox, ReceiptServer
from test_harness_actions_inbox import policy, receipt
from test_harness_direct_pr import DirectServiceTests, ISSUE
from test_harness_resident import resident


class StartupObserved(BaseException):
    pass


class ResidentStartupGateTests(unittest.TestCase):
    setUp = DirectServiceTests.setUp
    make = DirectServiceTests.make

    def setup_boot(self):
        self.inbox = Inbox(self.root / 'startup-inbox.sqlite')
        self.ipc = self.root / 'startup-ipc'
        self.ipc.mkdir(mode=0o711)
        self.socket_path = self.ipc / 'events.sock'
        self.servers = []
        self.reports = []
        self.query_window = []
        self.recovery_window = []
        self.ready_ack = None
        self.closed = Mock()

    def send(self, run):
        if not self.socket_path.exists():
            return {'unavailable': True}
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(3)
            peer.connect(str(self.socket_path))
            peer.sendall(json.dumps(receipt(run=run, issue=ISSUE)).encode() + b'\n')
            peer.shutdown(socket.SHUT_WR)
            with peer.makefile('rb') as incoming:
                return json.loads(incoming.readline())

    def claimed(self):
        binding = self.controller.approve(ISSUE)
        self.assertTrue(self.state.claim(binding, epoch=self.runtime.epoch))

    def run_boot(self, expected=StartupObserved, *, deny_claim_read=False):
        actual_recover = self.inbox.recover
        def recover():
            self.recovery_window.append(self.send(98))
            return actual_recover()
        def trace(sql):
            if sql == "SELECT 1 FROM service_runs WHERE phase='CLAIMED'":
                self.query_window.append(self.send(97))
        def authorizer(operation, table, column, database, trigger):
            if deny_claim_read and operation == sqlite3.SQLITE_READ and table == 'service_runs':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        def create_server(path, inbox, config):
            self.assertEqual(path, resident.SOCKET_PATH)
            server = ReceiptServer(self.socket_path, inbox, config, runner_uid=os.getuid())
            self.servers.append(server)
            return server
        def report(value):
            self.reports.append(value)
            if value['phase'] == 'RESIDENT_MANAGER_READY':
                self.ready_ack = self.send(99)
                raise StartupObserved()
        config = policy()
        state = SimpleNamespace(db=self.state.db, close=self.closed)
        self.state.db.set_trace_callback(trace)
        self.state.db.set_authorizer(authorizer)
        try:
            with patch.object(resident.os, 'getresuid', return_value=(10001,) * 3), \
                 patch.object(resident.os, 'umask'), \
                 patch.object(resident.sys, 'stdin', SimpleNamespace(buffer=SimpleNamespace(raw=io.BytesIO()))), \
                 patch.object(resident.common, 'read_frame', return_value={'epoch': self.runtime.epoch.to_dict()}), \
                 patch.object(resident.common, 'protected_json',
                              side_effect=lambda p: config if p.endswith('resident-intake.json') else {}), \
                 patch.object(resident.common, 'initialize_manager_state', return_value=self.root), \
                 patch.object(resident, 'ServiceState', return_value=state), \
                 patch.object(resident, 'Inbox', return_value=self.inbox), \
                 patch.object(self.inbox, 'recover', side_effect=recover), \
                 patch.object(resident, 'ReceiptServer', side_effect=create_server), \
                 patch.object(resident, 'report', side_effect=report):
                with self.assertRaises(expected):
                    resident.manager()
        finally:
            self.state.db.set_trace_callback(None)
            self.state.db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
        self.closed.assert_called_once_with()
        for server in self.servers:
            self.assertFalse(server.thread.is_alive())
        self.assertFalse(self.socket_path.exists())

    def test_uncertain_claim_has_no_listener_during_query_and_rejects_after_gate(self):
        self.setup_boot()
        self.claimed()
        before = (self.root / 'claims' / f'{ISSUE}.json').read_bytes()
        self.run_boot()
        self.assertEqual(self.query_window, [{'unavailable': True}])
        self.assertEqual(self.recovery_window, [])
        self.assertEqual(self.ready_ack, {'error': 'RECEIPT_REJECTED'})
        self.assertIn({'phase': 'RESIDENT_BLOCKED', 'reason': 'UNCERTAIN_PRIOR_CLAIM'}, self.reports)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'CLAIMED')
        self.assertEqual((self.root / 'claims' / f'{ISSUE}.json').read_bytes(), before)
        with self.inbox.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone(), (0,))
            self.assertEqual(db.execute('SELECT count(*) FROM gate').fetchone(), (1,))

    def test_recovery_finishes_before_listener_then_new_ack_follows_durable_commit(self):
        self.setup_boot()
        self.inbox.enqueue(receipt(run=1, issue=ISSUE))
        self.inbox.take()
        self.inbox.enqueue(receipt(run=2, issue=ISSUE))
        second = self.inbox.take()
        self.inbox.finish(second['receipt_id'], {'issue': ISSUE, 'phase': 'NOT_APPROVED', 'dispatched': False})
        self.run_boot()
        self.assertEqual(self.query_window, [{'unavailable': True}])
        self.assertEqual(self.recovery_window, [{'unavailable': True}])
        self.assertEqual(self.ready_ack['status'], 'enqueued')
        with sqlite3.connect(self.inbox.path) as db:
            self.assertEqual(db.execute('SELECT receipt,phase FROM events ORDER BY rowid').fetchall(),
                             [('actions:1312915009:1', 'PENDING'),
                              ('actions:1312915009:2', 'FINISHED'),
                              ('actions:1312915009:99', 'PENDING')])
        self.assertIsNone(self.state.get(ISSUE))

    def test_existing_inbox_latch_aborts_recovery_without_publishing_listener(self):
        self.setup_boot()
        self.inbox.block()
        from research_harness.actions_inbox import IntakeBlocked
        self.run_boot(expected=IntakeBlocked)
        self.assertEqual(self.servers, [])
        self.assertEqual(self.query_window, [{'unavailable': True}])
        self.assertEqual(self.recovery_window, [{'unavailable': True}])
        self.assertIsNone(self.ready_ack)

    def test_claim_query_failure_closes_state_without_publishing_listener(self):
        self.setup_boot()
        self.run_boot(expected=sqlite3.DatabaseError, deny_claim_read=True)
        self.assertEqual(self.servers, [])
        self.assertEqual(self.recovery_window, [])
        self.assertIsNone(self.ready_ack)

    def test_failed_durable_claim_gate_never_publishes_listener_or_success_ack(self):
        self.setup_boot()
        self.claimed()
        with self.inbox.connect() as db:
            db.execute("CREATE TRIGGER refuse_gate BEFORE INSERT ON gate "
                       "BEGIN SELECT RAISE(ABORT,'fixture gate persistence failure'); END")
        self.run_boot(expected=sqlite3.DatabaseError)
        self.assertEqual(self.servers, [])
        self.assertEqual(self.query_window, [{'unavailable': True}])
        self.assertEqual(self.recovery_window, [])
        self.assertIsNone(self.ready_ack)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'CLAIMED')
        with self.inbox.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone(), (0,))


del DirectServiceTests

if __name__ == '__main__':
    unittest.main()
