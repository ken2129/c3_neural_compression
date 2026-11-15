"""Actual read-only GitHub responses; all tests replay locally, never write GitHub.

Captures from PR78/80 validate REST shapes and hashes, not live goal publication.
Those PRs are legacy non-draft canaries and must NOT be adopted as goal PRs.
"""
import base64
from copy import deepcopy
import json
from pathlib import Path
import unittest

from research_harness.goal_artifacts import GoalEntry, GoalChange, GoalChangeCandidate
from research_harness.goal_result import GoalResult, GoalPublicationRequest
from research_harness.goal_publisher import GhGoalTransport, GitHubGoalPublisher, GoalPublicationState, _blob
from research_harness.result_publisher import RemoteConflict
from research_harness.schema import REPOSITORY


class RecordedGitHubGoalTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).parent / 'fixtures/goal_github_readonly_pr78_pr80.json'
        self.responses = json.loads(fixture.read_text())['responses']
        self.transport = GhGoalTransport(enabled=True, manager_credentials_isolated=True, runtime_verified=True)
        self.transport._executable = lambda: '/usr/bin/gh'
        self.calls = []
        def run(command, payload):
            self.assertEqual(command[command.index('--method') + 1], 'GET')
            self.assertEqual(payload, b'')
            route = command[-1].removeprefix('repos/' + REPOSITORY)
            self.calls.append(route)
            return 0, b'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n' + json.dumps(self.responses[route]).encode()
        self.transport._run = run

    def replay_request(self, number):
        pr = self.responses['/pulls/' + str(number)]
        base, head = pr['base']['sha'], pr['head']['sha']
        base_tree = self.responses['/git/commits/' + base]['tree']['sha']
        head_tree = self.responses['/git/commits/' + head]['tree']['sha']
        b = {e['path']: e for e in self.responses['/git/trees/' + base_tree + '?recursive=1']['tree'] if e['type'] != 'tree'}
        h = {e['path']: e for e in self.responses['/git/trees/' + head_tree + '?recursive=1']['tree'] if e['type'] != 'tree'}
        additions = [e for path,e in h.items() if path not in b]
        self.assertEqual(len(additions), 1)
        item = additions[0]
        blob = self.responses['/git/blobs/' + item['sha']]
        content = blob['connector_decoded_utf8'].encode('utf-8')
        self.assertEqual(len(content), item['size'])
        self.assertEqual(_blob(content), item['sha'])
        candidate = GoalChangeCandidate(base, ('docs/research/generated',), 'd' * 64, 'e' * 64,
            (GoalChange(item['path'], None, GoalEntry(item['path'], 'file', 0o644, content)),))
        result = GoalResult('fixture-request', 'fixture-run', 72, 'f' * 64, base,
                            'model-v1', 'completed', 'private', candidate, 1)
        return GoalPublicationRequest(result, base, '1' * 64), head, head_tree, base_tree

    def test_actual_git_commits_trees_and_blob_hashes(self):
        for number in (78,80):
            with self.subTest(number=number):
                request,head,tree,_ = self.replay_request(number)
                self.transport.verify_commit(request, head)
                self.assertEqual(self.transport.commit_tree(head), tree)
                self.assertGreater(len(self.transport._tree(tree)), 200)

    def test_truncated_recursive_tree_fails_closed(self):
        request,head,tree,_ = self.replay_request(80)
        self.responses['/git/trees/' + tree + '?recursive=1']['truncated'] = True
        with self.assertRaises(RemoteConflict): self.transport.verify_commit(request, head)

    def test_unchanged_symlink_executable_and_submodule_are_preserved(self):
        request,head,tree,base_tree = self.replay_request(80)
        extras = [dict(path='existing-link',mode='120000',type='blob',sha='1'*40),
                  dict(path='existing-executable',mode='100755',type='blob',sha='2'*40),
                  dict(path='existing-submodule',mode='160000',type='commit',sha='3'*40)]
        for sha in (tree,base_tree):
            self.responses['/git/trees/' + sha + '?recursive=1']['tree'].extend(deepcopy(extras))
        self.transport.verify_commit(request, head)
        self.responses['/git/trees/' + tree + '?recursive=1']['tree'][-1]['sha'] = '4'*40
        with self.assertRaises(RemoteConflict): self.transport.verify_commit(request, head)

    def test_actual_legacy_pr_is_rejected_but_goal_metadata_shape_works(self):
        request,head,tree,_ = self.replay_request(80)
        actual = self.transport._request('GET','/pulls/80')
        state = GoalPublicationState(request.digest,request.branch,'PR_UNKNOWN',tree,head)
        with self.assertRaises(RemoteConflict):
            GitHubGoalPublisher._verify_pr(request,state,actual)
        # Explicitly synthetic goal binding over recorded REST shape, not a
        # claim that a goal PR was posted or that the legacy PR is approved.
        goal = deepcopy(actual)
        goal.update(title=request.title,body=request.body,draft=True)
        goal['head']['ref'] = request.branch
        self.assertEqual(GitHubGoalPublisher._verify_pr(request,state,goal),80)

    def test_inline_utf8_tree_payload_matches_actual_blob_content(self):
        request,_,tree,base_tree = self.replay_request(80)
        original_run = self.transport._run
        sent = []
        def run(command,payload):
            if command[command.index('--method') + 1] == 'POST':
                self.assertEqual(command[-3], 'repos/' + REPOSITORY + '/git/trees')
                sent.append(json.loads(payload))
                return 0, b'HTTP/2.0 201 Created\r\n\r\n' + json.dumps({'sha':tree}).encode()
            return original_run(command,payload)
        self.transport._run = run
        self.assertEqual(self.transport.create_tree(request),tree)
        self.assertEqual(sent[0]['base_tree'],base_tree)
        entry = sent[0]['tree'][0]
        self.assertEqual(entry['mode'],'100644')
        self.assertEqual(entry['type'],'blob')
        self.assertEqual(entry['content'].encode('utf-8'),request.result.candidate.changes[0].after.content)

if __name__ == '__main__': unittest.main()
