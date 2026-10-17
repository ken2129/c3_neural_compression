"""GitHub adapter tests. Every transport and process is injected or mocked."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research_harness.plan_github import GitHubPlans
from research_harness.planning import ContractError, PlanDraft, PRSnapshot
from research_harness.schema import REPOSITORY
from test_research_harness_planning import issue, payload, SHA, HEAD, NEXT_HEAD


def encoded(value):
    return {'content': base64.b64encode(value.encode()).decode()}


class ReadAdapter(GitHubPlans):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.events = []
        self.commit_sha = SHA
        self.data = payload()
        self.markdown = PlanDraft.parse(self.data, issue()).markdown()
        self.changed = [{'filename': 'docs/research-plans/issue-42/' + name}
                        for name in ('plan.json', 'plan.md')]
        self.pr = dict(number=7, state='open', merged_at=None,
                       head={'ref': self.branch(42), 'sha': HEAD, 'repo': {'full_name': REPOSITORY}},
                       base={'ref': 'main', 'repo': {'full_name': REPOSITORY}},
                       body=self.pr_body(42), changed_files=2)

    def api(self, path, method='GET', data=None):
        self.calls.append((path, method, data))
        if method != 'GET':
            raise AssertionError('Read fixture must never mutate GitHub')
        if '/timeline?' in path:
            return copy.deepcopy(self.events)
        if path.endswith('/pulls/7'):
            return copy.deepcopy(self.pr)
        if '/pulls?' in path:
            return [copy.deepcopy(self.pr)]
        if '/pulls/7/files?' in path:
            return copy.deepcopy(self.changed)
        if '/contents/' in path and '/plan.json?' in path:
            return encoded(json.dumps(self.data))
        if '/contents/' in path and '/plan.md?' in path:
            return encoded(self.markdown)
        if '/issues/42' in path:
            return vars(issue())
        if '/commits/' in path:
            return {'sha': self.commit_sha}
        raise AssertionError('Unexpected endpoint: ' + path)


class GitHubReadTests(unittest.TestCase):
    def test_exact_body_base_paths_markdown_and_digest(self):
        adapter = ReadAdapter()
        result, = adapter.find_plan_prs(42)
        self.assertEqual(result.plan_digest, PlanDraft.parse(payload(), issue()).fingerprint)
        self.assertTrue(result.body_matches)
        self.assertEqual(result.head_sha, HEAD)
        paths = [p for p, _, _ in adapter.calls]
        self.assertIn(f'repos/{REPOSITORY}/contents/docs/research-plans/issue-42/plan.json?ref={HEAD}', paths)
        self.assertIn(f'repos/{REPOSITORY}/contents/docs/research-plans/issue-42/plan.md?ref={HEAD}', paths)
        self.assertTrue(all(method == 'GET' for _, method, _ in adapter.calls))

    def test_renamed_file_cannot_hide_out_of_scope_deletion(self):
        for extra in ({'status': 'renamed'}, {'previous_filename': '.github/workflows/ci.yml'}):
            adapter = ReadAdapter()
            adapter.changed[0].update(extra)
            with self.assertRaises(ContractError):
                adapter.find_plan_prs(42)

    def test_changed_body_is_detected_without_patch(self):
        adapter = ReadAdapter()
        adapter.pr['body'] += 'Unreviewed extra text'
        result, = adapter.find_plan_prs(42)
        self.assertFalse(result.body_matches)
        self.assertTrue(all(method == 'GET' for _, method, _ in adapter.calls))

    def test_wrong_base_owner_and_branch_rejected(self):
        for side, key, value in (('base', 'ref', 'develop'), ('head', 'ref', 'other'),
                                  ('base', 'repo', {'full_name': 'other/repo'}),
                                  ('head', 'repo', {'full_name': 'other/repo'})):
            adapter = ReadAdapter()
            adapter.pr[side][key] = value
            with self.subTest(side=side, key=key), self.assertRaises(ContractError):
                adapter.find_plan_prs(42)

    def test_unrelated_missing_duplicate_and_excess_files_rejected(self):
        for filenames in (['plan.json'], ['plan.json', 'plan.md', 'run.py'],
                          ['plan.json', 'plan.json']):
            adapter = ReadAdapter()
            adapter.changed = [{'filename': 'docs/research-plans/issue-42/' + name} for name in filenames]
            with self.subTest(filenames=filenames), self.assertRaises(ContractError):
                adapter.find_plan_prs(42)
        adapter = ReadAdapter()
        adapter.pr['changed_files'] = 101
        with self.assertRaises(ContractError):
            adapter.find_plan_prs(42)

    def test_plan_binding_schema_and_markdown_mismatch_rejected(self):
        for field, value in (('issue', 43), ('kind', 'execute'), ('source_sha', 'main')):
            adapter = ReadAdapter()
            adapter.data[field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                adapter.find_plan_prs(42)
        adapter = ReadAdapter()
        adapter.markdown += '\nInjected instruction\n'
        with self.assertRaises(ContractError):
            adapter.find_plan_prs(42)

    def test_cross_references_to_other_branch_or_repository_require_review(self):
        for url, branch in ((f'https://github.com/{REPOSITORY}/pull/7', 'unrelated'),
                            ('https://github.com/other/repo/pull/7', 'research-plan/issue-42')):
            adapter = ReadAdapter()
            adapter.events = [{'event': 'cross-referenced', 'source': {'issue': {
                'number': 7, 'pull_request': {}, 'html_url': url}}}]
            adapter.pr['head']['ref'] = branch
            with self.subTest(url=url), self.assertRaises(ContractError):
                adapter.find_plan_prs(42)

    def test_closed_and_merged_are_not_filtered_out(self):
        for state, merged, expected in (('closed', None, 'closed'), ('closed', 'timestamp', 'merged')):
            adapter = ReadAdapter()
            adapter.pr.update(state=state, merged_at=merged)
            result, = adapter.find_plan_prs(42)
            self.assertEqual(result.state, expected)

    def test_invalid_issue_id_never_reaches_api(self):
        adapter = ReadAdapter()
        for value in (True, 0, '42', '../secrets'):
            with self.subTest(value=value), self.assertRaises(ContractError):
                adapter.get_issue(value)
            with self.assertRaises(ContractError):
                adapter.branch(value)
        self.assertEqual(adapter.calls, [])

    def test_api_rejects_endpoint_outside_repository_before_process(self):
        with patch('research_harness.plan_github.subprocess.run') as run:
            with self.assertRaises(ContractError):
                GitHubPlans().api('repos/other/repo/issues/42')
            run.assert_not_called()


class GitHubWriteTests(unittest.TestCase):
    def setUp(self):
        self.plan = PlanDraft.parse(payload(), issue())
        self.kwargs = dict(issue=42, path=self.plan.path,
                           content=json.dumps(self.plan.to_dict()), body=self.plan.markdown(),
                           expected_head=None, proposal_digest=self.plan.fingerprint)

    def test_invalid_content_path_digest_body_and_head_never_run_git(self):
        for key, value in (('path', 'README.md'), ('proposal_digest', 'wrong'),
                           ('body', 'unreviewed'), ('expected_head', 'main')):
            adapter = ReadAdapter()
            with patch('research_harness.plan_github.subprocess.run') as run:
                with self.subTest(key=key), self.assertRaises(ContractError):
                    adapter.write_plan(**dict(self.kwargs, **{key: value}))
                run.assert_not_called()

    def test_unknown_source_commit_blocks_git_and_writes(self):
        adapter = ReadAdapter()
        adapter.commit_sha = HEAD
        with patch('research_harness.plan_github.subprocess.run') as run:
            with self.assertRaises(ContractError):
                adapter.write_plan(**self.kwargs)
            run.assert_not_called()
        self.assertIn((f'repos/{REPOSITORY}/commits/{SHA}', 'GET', None), adapter.calls)
        self.assertTrue(all(method == 'GET' for _, method, _ in adapter.calls))

    def test_existing_pr_metadata_mismatch_never_uses_patch(self):
        adapter = ReadAdapter()
        result = PRSnapshot(7, 42, 'open', NEXT_HEAD, self.plan.fingerprint, True, False)
        def fake_run(command, **kwargs):
            # No real subprocess, git operation or network request is performed.
            if 'rev-parse' in command:
                return SimpleNamespace(stdout=SHA + '\n')
            if 'ls-remote' in command:
                return SimpleNamespace(stdout='')
            return SimpleNamespace(stdout=NEXT_HEAD + '\n')
        with patch('research_harness.plan_github.subprocess.run', side_effect=fake_run), \
             patch.object(adapter, 'find_plan_prs', return_value=(result,)):
            with self.assertRaises(ContractError):
                adapter.write_plan(**self.kwargs)
        self.assertTrue(all(method == 'GET' for _, method, _ in adapter.calls))


class LocalBareGitIntegrationTests(unittest.TestCase):
    def test_create_update_and_stale_lease_against_local_bare_remote(self):
        """Run real git plumbing and lease pushes with all network protocols denied."""
        real_run = subprocess.run
        with tempfile.TemporaryDirectory(prefix='offline-plan-test-') as directory:
            remote = str(Path(directory) / 'remote.git')
            env = dict(os.environ, GIT_ALLOW_PROTOCOL='file', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
                       GIT_AUTHOR_NAME='Offline test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='Offline test', GIT_COMMITTER_EMAIL='test@example.invalid')

            def git(*args, input=None):
                return real_run(['git', '-c', 'core.hooksPath=/dev/null', '--git-dir', remote, *args],
                                input=input, text=True, capture_output=True, check=True,
                                timeout=30, env=env).stdout

            real_run(['git', 'init', '--bare', '--quiet', remote], check=True, env=env)
            blob = git('hash-object', '-w', '--stdin', input='Keep this baseline.\n').strip()
            tree = git('mktree', input=f'100644 blob {blob}\tREADME.md\n').strip()
            base = git('commit-tree', tree, input='Initial baseline\n').strip()
            git('update-ref', 'refs/heads/main', base)
            git('symbolic-ref', 'HEAD', 'refs/heads/main')
            commands = []

            def offline_run(command, **kwargs):
                if command[0] != 'git':
                    raise AssertionError('Only local git subprocesses are permitted')
                command = [remote if arg == f'https://github.com/{REPOSITORY}.git' else arg
                           for arg in command]
                if any('://' in arg for arg in command):
                    raise AssertionError('Network destination prohibited')
                commands.append(list(command))
                child_env = dict(kwargs.get('env', env))
                child_env.update(GIT_ALLOW_PROTOCOL='file', GIT_CONFIG_GLOBAL=os.devnull,
                                 GIT_CONFIG_NOSYSTEM='1')
                kwargs['env'] = child_env
                return real_run(command, **kwargs)

            class LocalAdapter(GitHubPlans):
                def __init__(self):
                    super().__init__()
                    self.created = False
                    self.api_calls = []

                def api(self, path, method='GET', data=None):
                    self.api_calls.append((path, method, data))
                    if path == f'repos/{self.repo}/issues/42' and method == 'GET':
                        return vars(issue())
                    if path == f'repos/{self.repo}/commits/{base}' and method == 'GET':
                        return {'sha': base}
                    if path == f'repos/{self.repo}/pulls' and method == 'POST':
                        self.created = True
                        return {'number': 7}
                    raise AssertionError('Unexpected fake API call: ' + path)

                def find_plan_prs(self, number):
                    if not self.created:
                        return ()
                    head = git('rev-parse', 'refs/heads/' + self.branch(number)).strip()
                    data = json.loads(git('show', f'{head}:docs/research-plans/issue-{number}/plan.json'))
                    return (PRSnapshot(7, number, 'open', head,
                                       PlanDraft.parse(data).fingerprint, True, True),)

            adapter = LocalAdapter()
            data = payload()
            data['source_sha'] = base
            first = PlanDraft.parse(data, issue())

            def write(plan, expected):
                adapter.write_plan(issue=42, path=plan.path, content=json.dumps(plan.to_dict()),
                                   body=plan.markdown(), expected_head=expected,
                                   proposal_digest=plan.fingerprint)

            with patch('research_harness.plan_github.subprocess.run', side_effect=offline_run):
                write(first, None)
                first_head = git('rev-parse', 'refs/heads/research-plan/issue-42').strip()
                paths = git('diff', '--name-only', base, first_head).splitlines()
                self.assertEqual(paths, [first.path, first.path.replace('.json', '.md')])
                self.assertEqual(git('show', f'{first_head}:README.md'), 'Keep this baseline.\n')
                self.assertEqual(json.loads(git('show', f'{first_head}:{first.path}')), first.to_dict())
                self.assertEqual(git('show', f"{first_head}:{first.path.replace('.json', '.md')}"), first.markdown())
                data['revision'] = 2
                data['sections']['summary'] = 'Revised reviewed proposal'
                second = PlanDraft.parse(data, issue())
                write(second, first_head)
                second_head = git('rev-parse', 'refs/heads/research-plan/issue-42').strip()
                self.assertNotEqual(first_head, second_head)
                self.assertEqual(git('rev-parse', second_head + '^').strip(), first_head)
                self.assertEqual(git('show', f'{second_head}:README.md'), 'Keep this baseline.\n')
                self.assertEqual(json.loads(git('show', f'{second_head}:{second.path}')), second.to_dict())
                pushes_before = sum('push' in cmd for cmd in commands)
                with self.assertRaises(ContractError):
                    write(second, first_head)
                self.assertEqual(sum('push' in cmd for cmd in commands), pushes_before)
                self.assertEqual(git('rev-parse', 'refs/heads/research-plan/issue-42').strip(), second_head)
            pushes = [cmd for cmd in commands if 'push' in cmd]
            self.assertEqual(len(pushes), 2)
            self.assertTrue(any(f'--force-with-lease=refs/heads/research-plan/issue-42:{first_head}' in cmd
                                for cmd in pushes))
            self.assertEqual(sum(method == 'POST' for _, method, _ in adapter.api_calls), 1)
            self.assertFalse(any(method == 'PATCH' for _, method, _ in adapter.api_calls))


if __name__ == '__main__':
    unittest.main()
