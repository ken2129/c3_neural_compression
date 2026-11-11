from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import unittest

from research_harness.goal_artifacts import GoalChangeCandidate, GoalChange, GoalEntry
from research_harness.goal_result import GoalResult, GoalPublicationRequest
from research_harness.goal_publisher import (GitHubGoalPublisher, GhGoalTransport,
    GoalPublicationState, matching_goal_publication_receipt, _blob)
from research_harness.owner_approval import MissingApproval, RevokedApproval
from research_harness.result_publisher import TransportUnknown, RemoteConflict
from research_harness.schema import ContractError, REPOSITORY, canonical

BASE = 'a' * 40
TREE = 'b' * 40
HEAD = 'c' * 40


def request():
    candidate = GoalChangeCandidate(BASE, ('out',), 'd' * 64, 'e' * 64,
        (GoalChange('out/a.txt', None, GoalEntry('out/a.txt', 'file', 0o644, b'reviewed a')),
         GoalChange('out/b.txt', None, GoalEntry('out/b.txt', 'file', 0o644, b'reviewed b'))))
    result = GoalResult('request-1', 'run-1', 72, 'f' * 64, BASE, 'model-v1',
                        'completed', 'PRIVATE summary', candidate, 1)
    return GoalPublicationRequest(result, BASE, '1' * 64)


class Fake:
    def __init__(self, req):
        self.req = req; self.base = BASE; self.branch = None; self.pr = None
        self.writes = []; self.fail = None; self.conflict = False
    def base_sha(self): return self.base
    def branch_sha(self, req): return self.branch
    def verify_tree(self, req, tree):
        if self.conflict: raise RemoteConflict('conflict')
    def verify_commit(self, req, head): self.verify_tree(req, TREE)
    def commit_tree(self, head): return TREE
    def write(self, kind):
        self.writes.append(kind)
        if self.fail == kind: raise TransportUnknown(reason='TIMEOUT')
    def create_tree(self, req): self.write('tree'); return TREE
    def create_commit(self, req, tree): self.write('commit'); return HEAD
    def create_branch(self, req, head): self.write('branch'); self.branch = head
    def find_pr(self, req): return self.pr
    def create_pr(self, req):
        self.write('pr')
        self.pr = dict(number=99, state='open', draft=True, merged=False,
            head=dict(ref=req.branch, sha=HEAD, repo=dict(full_name=REPOSITORY)),
            base=dict(ref='main', sha=BASE, repo=dict(full_name=REPOSITORY)),
            title=req.title, body=req.body, html_url=f'https://github.com/{REPOSITORY}/pull/99')


class GoalPublisherTests(unittest.TestCase):
    def setUp(self):
        self.request = request(); self.transport = Fake(self.request); self.saved = []
        self.publisher = GitHubGoalPublisher(self.transport, persist_state=self.saved.append)
    def publish(self, state=None, **kwargs):
        args = dict(authorized=True, authorize_callback=lambda: True); args.update(kwargs)
        return self.publisher.publish(self.request, state, **args)
    def test_multifile_single_commit_draft_and_durable_intents(self):
        state = self.publish()
        self.assertEqual(state.phase, 'PUBLISHED')
        self.assertEqual(self.transport.writes, ['tree', 'commit', 'branch', 'pr'])
        self.assertEqual([s.phase for s in self.saved], ['TREE_UNKNOWN','TREE_READY',
            'COMMIT_UNKNOWN','COMMIT_READY','BRANCH_UNKNOWN','BRANCH_READY','PR_UNKNOWN','PUBLISHED'])
        self.assertEqual(GoalPublicationState.from_dict(state.to_dict()), state)
    def test_no_approval_no_write(self):
        state = self.publish(authorized=False)
        self.assertEqual(state.reason, 'AUTHORIZATION_MISSING'); self.assertEqual(self.transport.writes, [])
    def test_approval_revocation_rechecked_every_write(self):
        count = 0
        def approve():
            nonlocal count
            count += 1
            return count == 1
        state = self.publish(authorize_callback=approve)
        self.assertEqual(state.reason, 'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.writes, ['tree'])
    def test_expired_review_pauses_and_fresh_review_resumes_without_duplicate(self):
        count = 0
        def expire():
            nonlocal count
            count += 1
            return count == 1
        state = self.publish(authorize_callback=expire)
        self.assertEqual(state.phase, 'TREE_READY')
        self.assertEqual(state.reason, 'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.writes, ['tree'])
        self.assertEqual(self.publish(state).phase, 'PUBLISHED')
        self.assertEqual(self.transport.writes, ['tree', 'commit', 'branch', 'pr'])

    def test_base_change_blocks(self):
        self.transport.base = '9' * 40
        self.assertEqual(self.publish().reason, 'BASE_CHANGED')
        self.assertEqual(self.transport.writes, [])
    def test_all_unknown_writes_never_repeated_on_absence(self):
        for failure in ('tree','commit','branch','pr'):
            with self.subTest(failure=failure):
                self.setUp(); self.transport.fail = failure
                state = self.publish(); original = list(self.transport.writes)
                self.transport.fail = None
                second = self.publish(state)
                self.assertEqual(second.phase, failure.upper() + '_UNKNOWN')
                self.assertEqual(self.transport.writes, original)
    def test_branch_unknown_readback_and_existing_pr(self):
        self.transport.branch = HEAD
        self.transport.create_pr(self.request); self.transport.writes.clear()
        state = GoalPublicationState(self.request.digest, self.request.branch,
                                     'BRANCH_UNKNOWN', TREE, HEAD)
        result = self.publish(state, authorized=False)
        self.assertEqual(result.phase, 'PUBLISHED'); self.assertEqual(self.transport.writes, [])
    def test_existing_pr_new_journal_read_only(self):
        self.transport.branch = HEAD; self.transport.create_pr(self.request); self.transport.writes.clear()
        self.assertEqual(self.publish(authorized=False).phase, 'PUBLISHED')
        self.assertEqual(self.transport.writes, [])
    def test_pr_tamper_blocks(self):
        state = self.publish(); self.transport.pr['body'] = 'tampered'
        state = replace(state, phase='PR_UNKNOWN', pr_number=None)
        self.assertEqual(self.publish(state).reason, 'REMOTE_CONFLICT')
    def test_journal_persist_failure_prevents_write(self):
        def fail(state): raise OSError('disk')
        self.publisher.persist_state = fail
        with self.assertRaises(OSError): self.publish()
        self.assertEqual(self.transport.writes, [])
    def test_wrong_journal_rejected(self):
        state = GoalPublicationState('0' * 64, self.request.branch)
        with self.assertRaises(ContractError): self.publish(state)
    def test_route_separation(self):
        transport = GhGoalTransport(enabled=True, manager_credentials_isolated=True, runtime_verified=True)
        for method, route in [('PUT','/contents/arbitrary'),('POST','/issues/72/comments'),('PATCH','/git/refs/heads/main')]:
            with self.assertRaises(ContractError): transport._request(method, route)
    def test_tree_exactness_and_source_preimage(self):
        transport = GhGoalTransport()
        base_tree = '2' * 40
        base = {'unchanged.txt': ('100644','blob', '3' * 40)}
        transport._commit = lambda sha: dict(tree=dict(sha=base_tree))
        expected = dict(base)
        for c in self.request.result.candidate.changes:
            expected[c.path] = ('100644','blob',_blob(c.after.content))
        transport._tree = lambda sha: base if sha == base_tree else expected
        transport.verify_tree(self.request, TREE)
        expected['unexpected.txt'] = ('100644','blob','4' * 40)
        with self.assertRaises(RemoteConflict): transport.verify_tree(self.request, TREE)
        base['out/a.txt'] = ('100644','blob','5' * 40)
        with self.assertRaises(RemoteConflict): transport._expected(self.request)
    def test_tree_inline_post_multiple_files(self):
        transport = GhGoalTransport(); calls = []
        transport._expected = lambda req: ('2' * 40, {})
        def call(method, route, payload):
            calls.append((method,route,payload)); return dict(sha=TREE)
        transport._request = call
        self.assertEqual(transport.create_tree(self.request), TREE)
        self.assertEqual(len(calls), 1)
        self.assertEqual([x['content'] for x in calls[0][2]['tree']], ['reviewed a','reviewed b'])


class ContentReceiptTests(unittest.TestCase):
    def setUp(self):
        self.request = request(); self.now = datetime(2026,10,17,tzinfo=timezone.utc).timestamp()
        self.body = dict(schema='c3-goal-publication-approval-v1', repository=REPOSITORY,
            issue=72, request_digest=self.request.digest, base_sha=BASE,
            candidate_digest=self.request.candidate_digest,
            issue_content_sha256=self.request.issue_content_sha256, source_sha=BASE,
            expires_at='2026-10-18T00:00:00Z', decision='approve-publication')
        self.comment = dict(id='node1', body=canonical(self.body).decode(),
            createdAt='2026-10-16T00:00:00Z', updatedAt='2026-10-16T00:00:00Z',
            lastEditedAt=None, editor=None, isMinimized=False,
            author=dict(databaseId=55587773,login='ken2129'))
    def check(self, comments=None):
        return matching_goal_publication_receipt(self.request, [self.comment] if comments is None else comments, self.now)
    def test_exact_receipt(self): self.assertEqual(self.check(),self.body)
    def test_spoof_edit_hidden_expired_and_execution_approval_rejected(self):
        for field, value in [('author',dict(databaseId=1,login='ken2129')),
                             ('updatedAt','2026-10-16T01:00:00Z'),('editor',dict(login='ken2129')),
                             ('isMinimized',True)]:
            changed=deepcopy(self.comment); changed[field]=value
            with self.assertRaises(MissingApproval): self.check([changed])
        for field, value in [('candidate_digest','0'*64),('expires_at','2026-10-16T00:00:00Z'),
                             ('decision','approve-execution'),('source_sha','0'*40)]:
            body=dict(self.body); body[field]=value
            changed=dict(self.comment,body=canonical(body).decode())
            with self.assertRaises(MissingApproval): self.check([changed])
    def test_duplicate_or_cancel_denied(self):
        with self.assertRaises(MissingApproval): self.check([self.comment,self.comment])
        body=dict(self.body,decision='cancel-publication')
        with self.assertRaises(RevokedApproval):
            self.check([self.comment,dict(self.comment,body=canonical(body).decode())])

if __name__ == '__main__': unittest.main()
