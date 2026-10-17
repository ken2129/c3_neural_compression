import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from unittest import mock
import io
import urllib.request
import tempfile
import unittest

from research_harness.connection_probe import (
    ContractError, PLAN_PATH, PURPOSE, REPOSITORY, acknowledge,
    approval_prompt, digest, parse_plan, parse_receipt, PublicGitHubReader, NoRedirect, MAX_BYTES,
)
from research_harness.store import Ledger, LedgerError


class Reader:
    def __init__(self):
        self.plan = {'schema': 'connection-probe-plan-v1', 'purpose': PURPOSE,
                     'request_id': 'probe-001', 'revision': 1,
                     'repository': REPOSITORY, 'expires_at': 2000, 'execute': False}
        repo = {'full_name': REPOSITORY}
        self.pr = {'number': 61, 'state': 'open', 'merged': False,
                   'head': {'sha': 'a' * 40, 'repo': repo}, 'base': {'repo': repo}}
        self.receipt = {'schema': 'connection-probe-receipt-v1', 'purpose': PURPOSE,
                        'request_id': 'probe-001', 'revision': 1, 'repository': REPOSITORY,
                        'pr_number': 61, 'head_sha': 'a' * 40, 'plan_digest': digest(self.plan),
                        'decision': 'approve-test-only', 'execute': False}
        self.comment = {'id': 42, 'issue_url': f'https://api.github.com/repos/{REPOSITORY}/issues/61',
                        'created_at': '2026-10-17T00:00:00Z', 'updated_at': '2026-10-17T00:00:00Z'}
        self.calls = []
        self.change_on_second_read = False

    def get(self, suffix):
        self.calls.append(suffix)
        if suffix == 'pulls/61':
            value = deepcopy(self.pr)
            if self.change_on_second_read and self.calls.count(suffix) == 2:
                value['head']['sha'] = 'b' * 40
            return value
        if suffix.startswith(f'contents/{PLAN_PATH}?ref='):
            return {'path': PLAN_PATH, 'encoding': 'base64',
                    'content': base64.b64encode(json.dumps(self.plan).encode()).decode()}
        if suffix == 'issues/comments/42':
            return dict(self.comment, body=json.dumps(self.receipt))
        raise AssertionError(suffix)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'probe.sqlite'
        self.reader = Reader()

    def receive(self, **kwargs):
        return acknowledge(self.db, 61, 42, kwargs.get('digest', digest(self.reader.receipt)),
                           self.reader, clock=kwargs.get('clock', lambda: kwargs.get('now', 1000)))

    def test_snapshot_contract_and_duplicate_are_test_only(self):
        first = self.receive()
        self.assertEqual(first, self.receive())
        self.assertIs(first['execute'], False)
        self.assertEqual(first['status'], 'RECEIVED_TEST_ONLY')
        self.assertEqual(len(self.reader.calls), 8)
        with self.assertRaises((ContractError, LedgerError)):
            Ledger(Path(self.tmp.name) / 'execution.sqlite').ingest_approval(first)

    def test_expiry_after_network_read(self):
        with self.assertRaises(ContractError):
            self.receive(clock=mock.Mock(side_effect=[1999, 2000]))

    def test_public_transport_get_only(self):
        reader = PublicGitHubReader()
        reader.opener = mock.Mock()
        reader.opener.open.return_value.__enter__ = mock.Mock(return_value=io.BytesIO(b'{"ok":true}'))
        reader.opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        self.assertEqual(reader.get('pulls/61'), {'ok': True})
        req = reader.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), 'GET')
        self.assertIsNone(req.data)
        self.assertNotIn('Authorization', req.headers)
        self.assertEqual(req.full_url, f'https://api.github.com/repos/{REPOSITORY}/pulls/61')

    def test_transport_size_limit(self):
        reader = PublicGitHubReader()
        reader.opener = mock.Mock()
        reader.opener.open.return_value.__enter__ = mock.Mock(return_value=io.BytesIO(b' ' * (MAX_BYTES + 1)))
        reader.opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with self.assertRaises(ContractError): reader.get('pulls/61')

    def test_redirect_denied(self):
        with self.assertRaises(ContractError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example')

    def test_malformed_transport_data(self):
        for raw in (b'not JSON', b'{"a":1,"a":2}', b'\xff'):
            with self.subTest(raw=raw):
                reader = PublicGitHubReader()
                reader.opener = mock.Mock()
                reader.opener.open.return_value.__enter__ = mock.Mock(return_value=io.BytesIO(raw))
                reader.opener.open.return_value.__exit__ = mock.Mock(return_value=False)
                with self.assertRaises((ContractError, UnicodeError)):
                    reader.get('pulls/61')

    def test_concurrent_receivers_store_one_ack(self):
        import sqlite3
        def receive(_):
            reader = Reader()
            return acknowledge(self.db, 61, 42, digest(reader.receipt), reader, clock=lambda: 1000)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(receive, range(8)))
        self.assertTrue(all(r == results[0] for r in results))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM probe_receipts').fetchone()[0], 1)

    def test_changed_head(self):
        self.reader.pr['head']['sha'] = 'b' * 40
        with self.assertRaises(ContractError): self.receive()

    def test_changed_during_read(self):
        self.reader.change_on_second_read = True
        with self.assertRaises(ContractError): self.receive()

    def test_expiry_boundary(self):
        with self.assertRaises(ContractError): self.receive(now=2000)

    def test_closed(self):
        self.reader.pr['state'] = 'closed'
        with self.assertRaises(ContractError): self.receive()

    def test_merged(self):
        self.reader.pr['merged'] = True
        with self.assertRaises(ContractError): self.receive()

    def test_fork(self):
        self.reader.pr['head']['repo'] = {'full_name': 'other/repo'}
        with self.assertRaises(ContractError): self.receive()

    def test_unknown_plan_field(self):
        self.reader.plan['command'] = 'echo forbidden'
        with self.assertRaises(ContractError): self.receive()

    def test_execution_plan_rejected(self):
        self.reader.plan['execute'] = True
        with self.assertRaises(ContractError): self.receive()

    def test_receipt_execution_rejected(self):
        self.reader.receipt['execute'] = True
        with self.assertRaises(ContractError): self.receive()

    def test_wrong_comment_pr(self):
        self.reader.comment['issue_url'] += '9'
        with self.assertRaises(ContractError): self.receive()

    def test_edited_receipt(self):
        self.reader.comment['updated_at'] = '2026-10-17T00:01:00Z'
        with self.assertRaises(ContractError): self.receive()

    def test_wrong_digest(self):
        with self.assertRaises(ContractError): self.receive(digest='0' * 64)

    def test_wrong_revision(self):
        self.reader.receipt['revision'] = 2
        with self.assertRaises(ContractError): self.receive()

    def test_wrong_plan_digest(self):
        self.reader.receipt['plan_digest'] = 'b' * 64
        with self.assertRaises(ContractError): self.receive()

    def test_denial(self):
        self.reader.receipt['decision'] = 'deny'
        with self.assertRaises(ContractError): self.receive()

    def test_unknown_receipt_field(self):
        self.reader.receipt['approval_token'] = 'forbidden'
        with self.assertRaises(ContractError): self.receive()

    def test_bool_revision(self):
        self.reader.receipt['revision'] = True
        with self.assertRaises(ContractError): self.receive()

    def test_duplicate_conflict(self):
        self.receive()
        self.reader.plan['revision'] = 2
        self.reader.receipt['revision'] = 2
        self.reader.receipt['plan_digest'] = digest(self.reader.plan)
        with self.assertRaises(ContractError): self.receive()

    def test_no_store_on_invalid(self):
        self.reader.pr['state'] = 'closed'
        with self.assertRaises(ContractError): self.receive()
        self.assertFalse(self.db.exists())

    def test_prompt_has_exact_target(self):
        text = approval_prompt(self.reader.plan, 61, 'a' * 40)
        self.assertIn('probe-001', text)
        self.assertIn('PR #61', text)
        self.assertIn('実験・GPU・マシン操作は行いません', text)


if __name__ == '__main__':
    unittest.main()
