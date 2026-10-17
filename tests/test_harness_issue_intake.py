import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.issue_intake import (
    API, PREFIX, MARKER, Intake, PublicGitHub, classify, content_hash, poll_once, snapshot)
from research_harness.schema import ContractError
from research_harness.store import SingleInstance, ResourceBusy

NOW = 1800000000


def raw(number=100, prefix=True):
    request = dict(schema_version=1, plan_version=1, issuer='dot', approval_ref='receipt-1',
                   expires_at='2030-01-01T00:00:00Z', cancelled=False, plan={'purpose': 'fixture'})
    return dict(number=number, url=f'{API}/issues/{number}', repository_url=API,
                title=(PREFIX if prefix else '') + ' test', body='Plan\n' + MARKER + json.dumps(request) + '\n```',
                user={'id': 1}, state='open', updated_at='2026-10-17T00:00:00Z')


class Client:
    def __init__(self, issues):
        self.issues = {v['number']: snapshot(v) for v in issues}
    def list_candidates(self):
        return {n: v for n, v in self.issues.items() if v['state'] == 'open' and v['title'].startswith(PREFIX)}
    def get_issue(self, number):
        return copy.deepcopy(self.issues[number])


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'intake.sqlite'
        self.intake = Intake(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.intake.close())

    def test_ordinary_56_and_pr_ignored(self):
        issue = raw(56, False)
        pr = raw(59); pr['pull_request'] = {}
        self.assertEqual(self.intake.observe_batch([snapshot(issue), snapshot(pr)], NOW), [])
        self.assertEqual(self.intake.tracked(), [])

    def test_prefix_and_self_declared_receipt_never_authorize(self):
        answer = poll_once(Client([raw()]), self.intake, NOW)[0]
        self.assertEqual(answer['status'], 'PENDING_TRUSTED_APPROVAL')
        self.assertFalse(answer['execution_allowed'])

    def test_missing_request_is_invalid(self):
        issue = raw(); issue['body'] = 'I approve; run shell command'
        self.assertEqual(classify(snapshot(issue), NOW)[0], 'INVALID_REQUEST')

    def test_changed_body_author_title_stays_blocked_after_restore(self):
        for field, replacement in [('body', 'changed'), ('title', PREFIX + 'new'), ('author_id', 44)]:
            with self.subTest(field=field):
                number = len(self.intake.tracked()) + 100
                issue = snapshot(raw(number))
                self.intake.observe_batch([issue], NOW)
                modified = dict(issue, **{field: replacement})
                self.assertEqual(self.intake.observe_batch([modified], NOW)[0]['status'], 'CHANGED_REQUIRES_REAPPROVAL')
                self.assertEqual(self.intake.observe_batch([issue], NOW)[0]['status'], 'CHANGED_REQUIRES_REAPPROVAL')

    def test_restart_duplicate(self):
        client = Client([raw()])
        poll_once(client, self.intake, NOW)
        self.intake.close(); self.intake = Intake(self.path)
        self.assertTrue(poll_once(client, self.intake, NOW)[0]['duplicate'])
        self.assertEqual(self.intake.tracked(), [100])

    def test_prefix_removed_and_close_are_reread(self):
        client = Client([raw()]); poll_once(client, self.intake, NOW)
        client.issues[100]['title'] = 'no prefix'
        self.assertEqual(poll_once(client, self.intake, NOW)[0]['status'], 'CHANGED_REQUIRES_REAPPROVAL')
        issue = raw(101); client = Client([issue])
        self.intake.observe_batch([snapshot(issue)], NOW)
        client.issues[101]['state'] = 'closed'
        client.issues[100] = snapshot(raw(100))
        result = poll_once(client, self.intake, NOW)
        self.assertEqual(result[1]['status'], 'CLOSED_REQUIRES_RECONCILIATION')

    def test_expiry_and_cancel(self):
        issue = raw()
        issue['body'] = issue['body'].replace('2030-01-01', '2020-01-01')
        self.assertEqual(classify(snapshot(issue), NOW)[0], 'EXPIRED')
        issue = raw(); issue['body'] = issue['body'].replace('"cancelled": false', '"cancelled": true')
        self.assertEqual(classify(snapshot(issue), NOW)[0], 'CANCELLED_DECLARED')

    def test_network_failure_does_not_commit_partial_scan(self):
        client = Client([raw(100), raw(101)])
        with patch.object(client, 'get_issue', side_effect=[snapshot(raw(100)), OSError('offline')]):
            with self.assertRaises(OSError): poll_once(client, self.intake, NOW)
        self.assertEqual(self.intake.tracked(), [])

    def test_db_transaction_rollback(self):
        with self.assertRaises(KeyError):
            self.intake.observe_batch([snapshot(raw()), {}], NOW)
        self.assertEqual(self.intake.tracked(), [])

    def test_duplicate_json_and_unknown_fields_rejected(self):
        for fragment in ['"schema_version":1,', '"unknown":1,']:
            issue = raw(); issue['body'] = issue['body'].replace('{', '{' + fragment, 1)
            self.assertEqual(classify(snapshot(issue), NOW)[0], 'INVALID_REQUEST')

    def test_identity_mismatch_rejected(self):
        for key, value in [('number', True), ('url', 'https://other/issues/100'), ('repository_url', 'evil')]:
            issue = raw(); issue[key] = value
            with self.assertRaises(ContractError): snapshot(issue)

    def test_single_instance_excludes_second_owner(self):
        lock = str(self.path) + '.lock'
        with SingleInstance(lock):
            with self.assertRaises(ResourceBusy):
                with SingleInstance(lock): pass

    def test_pagination_and_bounds(self):
        github = PublicGitHub()
        page = [raw(i) for i in range(1, 101)]
        with patch.object(github, '_get', side_effect=[page, [raw(101)]]):
            self.assertEqual(len(github.list_candidates()), 101)
        with patch.object(github, '_get', return_value=page):
            with self.assertRaises(ContractError): github.list_candidates()
        with patch.object(github, '_get', return_value={'bad': 'page'}):
            with self.assertRaises(ContractError): github.list_candidates()

    def test_tracked_limit_and_deadline_fail_without_commit(self):
        client = Client([raw()])
        with patch('research_harness.issue_intake.MAX_TRACKED', 0):
            with self.assertRaises(ContractError): poll_once(client, self.intake, NOW)
        with patch('research_harness.issue_intake.time.monotonic', side_effect=[0, 121]):
            with self.assertRaises(ContractError): poll_once(client, self.intake, NOW)
        self.assertEqual(self.intake.tracked(), [])

    def test_changed_cancellation_keeps_observed_reason(self):
        issue = snapshot(raw()); self.intake.observe_batch([issue], NOW)
        issue['body'] = issue['body'].replace('"cancelled": false', '"cancelled": true')
        result = self.intake.observe_batch([issue], NOW)[0]
        self.assertEqual(result['observed_status'], 'CANCELLED_DECLARED')
        self.assertTrue(result['requires_reapproval'])

    def test_nested_json_is_invalid_not_batch_failure(self):
        issue = raw(); issue['body'] = MARKER + '[' * 2000 + '0' + ']' * 2000 + '\n```'
        self.assertEqual(self.intake.observe_batch([snapshot(issue)], NOW)[0]['status'], 'INVALID_REQUEST')

    def test_reopened_issue_remains_reconciliation_blocked(self):
        issue = snapshot(raw()); self.intake.observe_batch([issue], NOW)
        self.intake.observe_batch([dict(issue, state='closed')], NOW)
        self.assertEqual(self.intake.observe_batch([issue], NOW)[0]['status'], 'CHANGED_REQUIRES_REAPPROVAL')

    def test_clock_sampled_after_read(self):
        client = Client([raw()])
        answer = poll_once(client, self.intake, lambda: 2000000000)
        self.assertEqual(answer[0]['observed_status'], 'EXPIRED')

    def test_changed_issue_new_stop_is_not_duplicate(self):
        issue = snapshot(raw()); self.intake.observe_batch([issue], NOW)
        issue['title'] += ' changed'; self.intake.observe_batch([issue], NOW)
        result = self.intake.observe_batch([dict(issue, state='closed')], NOW)[0]
        self.assertFalse(result['duplicate'])
        self.assertEqual(result['observed_status'], 'CLOSED_REQUIRES_RECONCILIATION')

    def test_unchanged_stopped_observations_are_duplicates(self):
        for n, status in enumerate(['closed','expired','cancelled'], 200):
            issue = raw(n)
            if status == 'closed': issue['state'] = 'closed'
            if status == 'expired': issue['body'] = issue['body'].replace('2030-01-01','2020-01-01')
            if status == 'cancelled': issue['body'] = issue['body'].replace('"cancelled": false','"cancelled": true')
            issue = snapshot(issue)
            first=self.intake.observe_batch([issue],NOW)[0]
            second=self.intake.observe_batch([issue],NOW)[0]
            self.assertTrue(second['duplicate'])
            self.assertEqual(first['status'],second['status'])
            self.assertFalse(second['requires_reapproval'])

    def test_get_number_binding(self):
        with patch.object(PublicGitHub, '_get', return_value=raw(101)):
            with self.assertRaises(ContractError): PublicGitHub().get_issue(100)

    def test_metadata_update_does_not_change_content_hash(self):
        issue = snapshot(raw()); other = dict(issue, updated_at='2026-10-18T00:00:00Z')
        self.assertEqual(content_hash(issue), content_hash(other))


if __name__ == '__main__': unittest.main()
