import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_harness.issue_intake import Intake, content_hash, snapshot
from research_harness.owner_approval import (OWNER_ID, OWNER_LOGIN, SCHEMA,
    GitHubOwnerComments, inspect_once, matching_receipt)
from research_harness.schema import ContractError
from test_harness_issue_intake import raw, NOW, Client


def issue_and_comment():
    data = raw(); data['body'] = data['body'].replace('"issuer": "dot"', '"issuer": "ken2129"')
    issue = snapshot(data)
    body = dict(schema=SCHEMA, request_id='receipt-1', repository='ken2129/c3_neural_compression',
                issue=100, plan_version=1, content_sha256=content_hash(issue),
                expires_at='2030-01-01T00:00:00Z', decision='approve-execution')
    comment = dict(id="IC_fixture_123", body=json.dumps(body), author={'databaseId': OWNER_ID, 'login': OWNER_LOGIN},
                   createdAt='2026-10-17T00:00:00Z', updatedAt='2026-10-17T00:00:00Z',
                   lastEditedAt=None, editor=None, isMinimized=False)
    return data, issue, comment


class Comments:
    def __init__(self, comment): self.comment = comment
    def comments(self, number): return [copy.deepcopy(self.comment)]


class OwnerApprovalTests(unittest.TestCase):
    def test_valid_owner_record_not_execution(self):
        data, issue, comment = issue_and_comment()
        with tempfile.TemporaryDirectory() as root:
            db = Intake(Path(root) / 'state.sqlite')
            try:
                result = inspect_once(Client([data]), Comments(comment), db, 100, lambda: NOW)
                self.assertEqual(result['approval'], 'OWNER_RECORD_MATCHED')
                self.assertFalse(result['execution_allowed'])
                again = inspect_once(Client([data]), Comments(comment), db, 100, lambda: NOW)
                self.assertTrue(again['duplicate'])
            finally: db.close()

    def test_fake_owner_and_missing_author_rejected(self):
        _, issue, comment = issue_and_comment()
        for author in [None, {}, {'login': OWNER_LOGIN, 'databaseId': 99}, {'databaseId': OWNER_ID, 'login': 'other'}]:
            bad = dict(comment, author=author)
            with self.assertRaises(ContractError): matching_receipt(issue, [bad], NOW)

    def test_edit_metadata_missing_or_edited_rejected(self):
        _, issue, comment = issue_and_comment()
        for key in ['editor', 'lastEditedAt', 'isMinimized', 'createdAt']:
            bad = dict(comment); del bad[key]
            with self.assertRaises(ContractError): matching_receipt(issue, [bad], NOW)
        for key, val in [('editor', {'login': OWNER_LOGIN}), ('lastEditedAt','2026-10-17T00:00:00Z'), ('isMinimized',True)]:
            with self.assertRaises(ContractError): matching_receipt(issue, [dict(comment, **{key:val})], NOW)

    def test_wrong_bindings_scope_and_revocation_rejected(self):
        _, issue, comment = issue_and_comment()
        for key, value in [('issue',101),('issue',True),('plan_version',2),('content_sha256','0'*64),
                           ('repository','other/repo'),('expires_at','2031-01-01T00:00:00Z'),
                           ('decision','approve-merge'),('decision','cancel-execution')]:
            body = json.loads(comment['body']); body[key] = value
            with self.assertRaises(ContractError): matching_receipt(issue,[dict(comment, body=json.dumps(body))],NOW)
        with self.assertRaises(ContractError): matching_receipt(issue,[comment],2000000000)

    def test_duplicate_approval_and_later_cancel_rejected(self):
        _, issue, comment = issue_and_comment()
        with self.assertRaises(ContractError): matching_receipt(issue,[comment,dict(comment,id="IC_fixture_124")],NOW)
        body=json.loads(comment['body']);body['decision']='cancel-execution'
        with self.assertRaises(ContractError): matching_receipt(issue,[comment,dict(comment,id="IC_fixture_124",body=json.dumps(body))],NOW)

    def test_change_during_read_rejected(self):
        data, issue, comment = issue_and_comment()
        client=Client([data]); changed=dict(issue,updated_at='2026-10-18T00:00:00Z')
        db=Intake(':memory:')
        try:
            with patch.object(client,'get_issue',side_effect=[issue,changed]):
                with self.assertRaises(ContractError): inspect_once(client,Comments(comment),db,100,lambda:NOW)
            self.assertEqual(db.tracked(),[])
        finally: db.close()

    def test_cached_receipt_replacement_rejected_after_restart(self):
        data, issue, comment = issue_and_comment()
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.sqlite'; db=Intake(path)
            inspect_once(Client([data]),Comments(comment),db,100,lambda:NOW);db.close()
            db=Intake(path)
            try:
                with self.assertRaises(ContractError):
                    inspect_once(Client([data]),Comments(dict(comment,id="IC_fixture_124")),db,100,lambda:NOW)
            finally: db.close()

    def test_revocation_stays_blocked_after_cancel_disappears(self):
        data, issue, comment = issue_and_comment()
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.sqlite'; db=Intake(path)
            inspect_once(Client([data]),Comments(comment),db,100,lambda:NOW)
            body=json.loads(comment['body']);body['decision']='cancel-execution'
            cancel=dict(comment,id="IC_fixture_124",body=json.dumps(body))
            with self.assertRaises(ContractError):inspect_once(Client([data]),Comments(cancel),db,100,lambda:NOW)
            db.close();db=Intake(path)
            try:
                with self.assertRaises(ContractError):inspect_once(Client([data]),Comments(comment),db,100,lambda:NOW)
            finally:db.close()

    def test_graphql_missing_metadata_errors_and_pagination(self):
        def response(nodes, more=False, cursor=None):
            return type('Result',(),{'stdout':json.dumps({'data':{'repository':{'issue':{'number':100,'comments':{'nodes':nodes,'pageInfo':{'hasNextPage':more,'endCursor':cursor}}}}}}).encode()})()
        adapter=GitHubOwnerComments()
        with patch('research_harness.owner_approval.subprocess.run',side_effect=[response([],True,'abc'),response([])]) as call:
            self.assertEqual(adapter.comments(100),[])
            self.assertEqual(call.call_args.args[0][:5], ['gh','api','--hostname','github.com','graphql'])
        with patch('research_harness.owner_approval.subprocess.run',return_value=response([],True,'abc')):
            with self.assertRaises(ContractError):adapter.comments(100)
        with patch('research_harness.owner_approval.subprocess.run',return_value=type('Result',(),{'stdout':b'{"errors":[{}]}'})()):
            with self.assertRaises(ContractError):adapter.comments(100)


if __name__ == '__main__':unittest.main()
