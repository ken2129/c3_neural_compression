"""Live resident switch with actual SQLite/approval checks; no model/network."""
from copy import deepcopy
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_harness.actions_inbox import Inbox, IntakeBlocked
from research_harness.direct_pr_service import DirectPRController
from research_harness.issue_intake import timestamp
from research_harness.schema import ContractError
from test_harness_actions_inbox import policy, receipt
from test_harness_direct_pr import (DirectServiceTests, direct_profile, direct_source,
    enabled_config, ISSUE, MultipleSources)
from test_harness_resident import resident
from tests.test_harness_goal_deployment import prepare


class StopManager(BaseException):
    pass


class ResidentSwitchTests(unittest.TestCase):
    setUp = DirectServiceTests.setUp
    make = DirectServiceTests.make

    def setup_loop(self, *, enabled=False):
        self.live = prepare.installed_profile(direct_profile(self.source.request['plan']),
                                              self.source.request['plan'])
        self.live['enabled'] = enabled
        self.inbox = Inbox(self.root / 'switch-inbox.sqlite')
        self.waits = []
        self.reports = []
        self.runtime.failed_issue = None
        self.read_error = None
        self.wait_hook = lambda: (_ for _ in ()).throw(StopManager())

    def phases(self):
        with self.inbox.connect() as db:
            return db.execute('SELECT phase FROM events ORDER BY rowid').fetchall()

    def run_loop(self, *, take_hook=None):
        def read(path):
            if path.endswith('resident-intake.json'):
                return policy()
            if path.endswith('worker-pr.json'):
                return enabled_config()
            if self.read_error is not None:
                raise self.read_error
            return deepcopy(self.live)
        def wait(timeout=None):
            self.waits.append(timeout)
            self.wait_hook()
        def controller(*args, **kwargs):
            return DirectPRController(*args, **kwargs, clock=lambda: self.now,
                                      monotonic=lambda: self.mono)
        real_take = self.inbox.take
        def take():
            value = real_take()
            if value is not None and take_hook is not None:
                take_hook()
            return value
        frames = [{'epoch': self.runtime.epoch.to_dict()}] + [{'cleaned': True}] * 4
        server = SimpleNamespace(start=Mock(), close=Mock(),
                                 thread=SimpleNamespace(is_alive=lambda: True))
        self.inbox.wake = SimpleNamespace(set=Mock(), clear=Mock(), wait=wait)
        with patch.object(resident.os, 'getresuid', return_value=(10001,) * 3), \
             patch.object(resident.os, 'umask'), \
             patch.object(resident.sys, 'stdin', SimpleNamespace(buffer=SimpleNamespace(raw=io.BytesIO()))), \
             patch.object(resident.sys, 'stdout', SimpleNamespace(buffer=io.BytesIO())), \
             patch.object(resident.common, 'read_frame', side_effect=lambda _: frames.pop(0)), \
             patch.object(resident.common, 'protected_json', side_effect=read), \
             patch.object(resident.common, 'initialize_manager_state', return_value=self.root), \
             patch.object(resident, 'ServiceState', return_value=self.state), \
             patch.object(self.state, 'close') as closed, \
             patch.object(resident, 'Inbox', return_value=self.inbox), \
             patch.object(self.inbox, 'take', side_effect=take), \
             patch.object(resident, 'ReceiptServer', return_value=server), \
             patch.object(resident, 'ResidentGoalPipeRuntime', return_value=self.runtime), \
             patch.object(resident, 'DirectPRController', side_effect=controller), \
             patch.object(resident, 'ManagerGitHub', return_value=self.issues), \
             patch.object(resident, 'GitHubOwnerComments', return_value=self.issues), \
             patch.object(resident, 'report', side_effect=self.reports.append):
            with self.assertRaises(StopManager):
                resident.manager()
        closed.assert_called_once_with()
        server.close.assert_called_once_with()

    def test_off_keeps_delivery_pending_and_on_resumes_without_new_event(self):
        self.setup_loop()
        self.inbox.enqueue(receipt(issue=ISSUE))
        def wait():
            self.assertEqual(self.waits[-1], 1)
            if len(self.waits) == 1:
                self.assertEqual(self.phases(), [('PENDING',)])
                self.assertIsNone(self.state.get(ISSUE))
                self.assertEqual(self.runtime.runs, 0)
                self.live['enabled'] = True
            else:
                self.assertEqual(self.phases(), [('FINISHED',)])
                self.assertEqual(self.state.get(ISSUE)['phase'], 'WORKER_FINISHED')
                raise StopManager()
        self.wait_hook = wait
        self.run_loop()
        self.assertEqual(self.runtime.runs, 1)
        self.assertIn({'phase': 'RESIDENT_AUTOMATION_OFF'}, self.reports)
        self.assertIn({'phase': 'RESIDENT_AUTOMATION_ON'}, self.reports)

    def test_off_after_dequeue_releases_lease_without_claim_or_consuming_event(self):
        self.setup_loop(enabled=True)
        self.inbox.enqueue(receipt(issue=ISSUE))
        self.run_loop(take_hook=lambda: self.live.update(enabled=False))
        self.assertEqual(self.phases(), [('PENDING',)])
        self.assertIsNone(self.state.get(ISSUE))
        self.assertEqual(self.runtime.runs, 0)

    def test_off_during_run_finishes_active_job_but_leaves_next_pending(self):
        self.setup_loop(enabled=True)
        second = direct_source(ISSUE + 1)
        self.issues = MultipleSources(self.source, second)
        self.inbox.enqueue(receipt(issue=ISSUE))
        self.inbox.enqueue(receipt(run=2, issue=ISSUE + 1))
        self.runtime.on_run = lambda: self.live.update(enabled=False)
        self.run_loop()
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'WORKER_FINISHED')
        self.assertIsNone(self.state.get(ISSUE + 1))
        self.assertEqual(self.phases(), [('FINISHED',), ('PENDING',)])

    def test_off_during_preflight_cleans_then_defers_and_resumes_without_claim_reset(self):
        self.setup_loop(enabled=True)
        self.inbox.enqueue(receipt(issue=ISSUE))
        def paused():
            self.live['enabled'] = False
            raise resident.ResidentExecutionPaused('resident execution paused')
        self.runtime.on_preflight = paused
        def wait():
            if len(self.waits) == 1:
                self.assertEqual(self.phases(), [('PENDING',)])
                self.assertIsNone(self.state.get(ISSUE))
                self.assertEqual(self.runtime.runs, 0)
                self.live['enabled'] = True
                self.runtime.on_preflight = lambda: None
            else:
                raise StopManager()
        self.wait_hook = wait
        self.run_loop()
        self.assertEqual(self.runtime.preflights, 2)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'WORKER_FINISHED')
        self.assertEqual(self.phases(), [('FINISHED',)])

    def test_off_after_successful_preflight_preserves_admitted_job_and_pauses_next(self):
        self.setup_loop(enabled=True)
        second = direct_source(ISSUE + 1)
        self.issues = MultipleSources(self.source, second)
        self.inbox.enqueue(receipt(issue=ISSUE))
        self.inbox.enqueue(receipt(run=2, issue=ISSUE + 1))
        self.runtime.on_preflight = lambda: self.live.update(enabled=False)
        self.run_loop()
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'WORKER_FINISHED')
        self.assertIsNone(self.state.get(ISSUE + 1))
        self.assertEqual(self.phases(), [('FINISHED',), ('PENDING',)])

    def test_expired_approval_is_rechecked_after_on_and_never_claimed(self):
        self.setup_loop()
        self.inbox.enqueue(receipt(issue=ISSUE))
        def wait():
            if len(self.waits) == 1:
                self.now = timestamp(self.source.request['expires_at'])
                self.live['enabled'] = True
            else:
                raise StopManager()
        self.wait_hook = wait
        self.run_loop()
        self.assertEqual(self.phases(), [('FINISHED',)])
        self.assertIsNone(self.state.get(ISSUE))
        self.assertEqual(self.runtime.runs, 0)

    def test_invalid_live_configuration_pauses_without_claim_and_can_be_repaired(self):
        self.setup_loop(enabled=True)
        self.inbox.enqueue(receipt(issue=ISSUE))
        original = deepcopy(self.live)
        def wait():
            if len(self.waits) == 1:
                self.assertEqual(self.phases(), [('PENDING',)])
                self.assertIsNone(self.state.get(ISSUE))
                self.live = original
            else:
                raise StopManager()
        self.wait_hook = wait
        original_recover = self.inbox.recover
        def recover():
            result = original_recover()
            self.live['enabled'] = 'true'
            return result
        with patch.object(self.inbox, 'recover', side_effect=recover):
            self.run_loop()
        self.assertEqual(self.runtime.runs, 1)
        self.assertIn({'phase': 'RESIDENT_AUTOMATION_CONFIG_REJECTED'}, self.reports)

    def test_profile_read_failure_defers_already_dequeued_event(self):
        self.setup_loop(enabled=True)
        self.inbox.enqueue(receipt(issue=ISSUE))
        self.run_loop(take_hook=lambda: setattr(self, 'read_error', PermissionError('private error')))
        self.assertEqual(self.phases(), [('PENDING',)])
        self.assertEqual(self.runtime.runs, 0)
        self.assertIsNone(self.state.get(ISSUE))

    def test_only_strict_enabled_boolean_can_change_without_restart(self):
        self.setup_loop()
        installed = deepcopy(self.live)
        for enabled in (True, False):
            self.live['enabled'] = enabled
            with patch.object(resident.common, 'protected_json', return_value=deepcopy(self.live)):
                self.assertIs(resident.dispatch_profile(installed)['enabled'], enabled)
        for value in (0, 1, None, 'false', 'true'):
            changed = dict(installed, enabled=value)
            with self.subTest(enabled=value), patch.object(resident.common, 'protected_json', return_value=changed):
                with self.assertRaises(ContractError):
                    resident.dispatch_profile(installed)
        for key in ('model', 'resource_limits', 'deployment_id', 'risk_acknowledgements'):
            changed = deepcopy(installed)
            changed[key] = 'unexpected-change'
            with self.subTest(key=key), patch.object(resident.common, 'protected_json', return_value=changed):
                with self.assertRaises(ContractError):
                    resident.dispatch_profile(installed)

    def test_defer_cannot_reset_finished_delivery_or_cleanup_latch(self):
        self.setup_loop()
        value = receipt(issue=ISSUE)
        self.inbox.enqueue(value)
        self.inbox.take()
        self.inbox.finish(value['receipt_id'], {'phase': 'WORKER_FINISHED'})
        with self.assertRaises(IntakeBlocked):
            self.inbox.defer(value['receipt_id'])
        self.inbox.enqueue(receipt(run=2, issue=ISSUE + 1))
        active = self.inbox.take()
        self.inbox.block()
        self.inbox.defer(active['receipt_id'])
        with self.assertRaises(IntakeBlocked):
            self.inbox.take()


del DirectServiceTests

if __name__ == '__main__':
    unittest.main()
