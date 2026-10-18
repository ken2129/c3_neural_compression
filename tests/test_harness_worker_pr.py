from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from research_harness.schema import ContractError, REPOSITORY
from research_harness.worker_pr import (
    CONSENTS, GitHubFailure, MAX_FILE_BYTES, MAX_JSON_BYTES, OWNER_ID,
    WorkerGitHub, default_worker_pr_config, main, prepare_publication, publish,
    validate_worker_pr_config,
)

BASE = 'a' * 40
BASE_TREE = 'b' * 40
TREE = 'c' * 40
HEAD = 'd' * 40
ACTOR = dict(login='c3-research-bot', id=1234567)


def config():
    return dict(default_worker_pr_config(), enabled=True, actor_login=ACTOR['login'],
                actor_id=ACTOR['id'], **dict.fromkeys(CONSENTS, True))


def plan():
    return dict(write_scope=['outputs'], direct_pr=dict(repository=REPOSITORY,
        issue_number=72, base_branch='main', base_sha=BASE,
        title='Evaluate a bounded goal', public_results_approved=True))


class FakeGitHub:
    def __init__(self, publication, intent):
        self.p = publication
        self.intent = Path(intent)
        self.actor = deepcopy(ACTOR)
        self.base = BASE
        self.branch = None
        self.pr = None
        self.writes = []
        self.reads = []
        self.fail = None
        self.fail_after_effect = False
        self.extra_diff = False
        self.tree_mode = None

    def request(self, method, path, payload=None, *, missing_ok=False):
        suffix = path.removeprefix(f'/repos/{REPOSITORY}/')
        if method == 'GET':
            self.reads.append(path)
            if path == '/user':
                return self.actor
            if suffix == 'git/ref/heads/main':
                return dict(ref='refs/heads/main', object=dict(type='commit', sha=self.base))
            if suffix == 'git/ref/heads/' + self.p.branch:
                return None if self.branch is None else dict(
                    ref='refs/heads/' + self.p.branch, object=dict(type='commit', sha=self.branch))
            if suffix.startswith('pulls?'):
                return [] if self.pr is None else [self.pr]
            if suffix == 'git/commits/' + BASE:
                return dict(sha=BASE, tree=dict(sha=BASE_TREE))
            if suffix == 'git/commits/' + HEAD:
                return dict(sha=HEAD, message=self.p.title + '\n\n' + self.p.body,
                            parents=[dict(sha=BASE)], tree=dict(sha=TREE))
            if suffix.startswith('compare/'):
                changed = [dict(filename=p, status='modified', sha=s) for p, _, _, s in self.p.files]
                if self.extra_diff:
                    changed.append(dict(filename='other.txt', status='added', sha='e' * 40))
                return dict(total_commits=1, files=changed)
            if suffix == 'git/trees/' + TREE + '?recursive=1':
                return dict(truncated=False, tree=[dict(path=p, type='blob', mode=self.tree_mode or m, sha=s)
                            for p, m, _, s in self.p.files])
            raise AssertionError(path)
        self.assert_intent()
        self.writes.append((suffix, deepcopy(payload)))
        if suffix == self.fail and not self.fail_after_effect:
            raise GitHubFailure('outcome unknown')
        if suffix == 'git/blobs':
            import base64
            data = base64.b64decode(payload['content'])
            response = dict(sha=hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest())
        elif suffix == 'git/trees':
            response = dict(sha=TREE)
        elif suffix == 'git/commits':
            response = dict(sha=HEAD)
        elif suffix == 'git/refs':
            self.branch = payload['sha']
            response = dict(ref=payload['ref'])
        elif suffix == 'pulls':
            self.pr = dict(number=99, user=deepcopy(ACTOR), title=payload['title'],
                body=payload['body'], draft=payload['draft'], state='open',
                head=dict(ref=self.p.branch, sha=HEAD, repo=dict(full_name=REPOSITORY)),
                base=dict(ref='main', repo=dict(full_name=REPOSITORY)),
                html_url=f'https://github.com/{REPOSITORY}/pull/99')
            response = self.pr
        else:
            raise AssertionError(path)
        if suffix == self.fail:
            raise GitHubFailure('outcome unknown after write')
        return response

    def assert_intent(self):
        data = json.loads(self.intent.read_text())
        assert data['digest'] == self.p.digest
        assert os.stat(self.intent).st_mode & 0o777 == 0o600


class WorkerPRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'outputs').mkdir()
        (self.root / 'outputs/a.txt').write_bytes(b'bounded output\n')
        self.manifest = dict(files=['outputs/a.txt'])
        self.plan = plan()
        self.intent = self.root / 'intent.json'
        self.publication = prepare_publication(self.plan, self.manifest, self.root)
        self.client = FakeGitHub(self.publication, self.intent)

    def publish(self, **kwargs):
        values = dict(config=config(), approved_plan=self.plan, manifest=self.manifest,
                      work_root=self.root, intent_path=self.intent, client=self.client)
        values.update(kwargs)
        return publish(**values)

    def test_disabled_by_default_and_flags_are_strict(self):
        self.assertFalse(validate_worker_pr_config(None)['enabled'])
        self.assertFalse(validate_worker_pr_config(dict(schema='c3-worker-pr-v1'))['enabled'])
        with self.assertRaises(ContractError):
            self.publish(config=default_worker_pr_config())
        self.assertEqual(self.client.reads, [])
        for flag in ('enabled', *CONSENTS):
            with self.subTest(flag=flag), self.assertRaises(ContractError):
                validate_worker_pr_config(dict(config(), **{flag: 1}))

    def test_enabled_requires_dedicated_non_owner_and_all_consents(self):
        for change in [dict(actor_login='KEN2129'), dict(actor_id=OWNER_ID), dict(actor_id=True),
                       dict(actor_id=0), dict(actor_login=None), dict(actor_id=None),
                       *(dict(**{key: False}) for key in CONSENTS)]:
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_worker_pr_config(dict(config(), **change))
        self.assertEqual(validate_worker_pr_config(config())['actor_id'], ACTOR['id'])

    def test_unknown_config_fields_and_other_repository_rejected(self):
        for change in [dict(token_path='/control/github-token'), dict(repository='someone/else'),
                       dict(schema='another')]:
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_worker_pr_config(dict(config(), **change))

    def test_exact_direct_pr_policy_and_scope_required(self):
        for change in [dict(repository='another/repo'), dict(base_branch='feature'),
                       dict(base_sha='main'), dict(issue_number=True), dict(public_results_approved=False),
                       dict(title='hello\nbody'), dict(unexpected=True)]:
            bad = deepcopy(self.plan)
            bad['direct_pr'].update(change)
            with self.subTest(change=change), self.assertRaises(ContractError):
                prepare_publication(bad, self.manifest, self.root)
        for path in ['../a', '/tmp/a', '.git/config', 'outputs/../../a', 'outputs/*', 'elsewhere.txt']:
            with self.subTest(path=path), self.assertRaises(ContractError):
                prepare_publication(self.plan, dict(files=[path]), self.root)
        with self.assertRaises(ContractError):
            prepare_publication(self.plan, dict(files=['outputs/a.txt'] * 2), self.root)

    def test_symlinks_hardlinks_special_and_oversized_files_rejected(self):
        target = self.root / 'outputs/b.txt'
        cases = [lambda: target.symlink_to('a.txt'),
                 lambda: os.link(self.root / 'outputs/a.txt', target),
                 lambda: os.mkfifo(target),
                 lambda: target.write_bytes(b'x' * (MAX_FILE_BYTES + 1))]
        for create in cases:
            create()
            with self.assertRaises((ContractError, OSError)):
                prepare_publication(self.plan, dict(files=['outputs/b.txt']), self.root)
            target.unlink()
        (self.root / 'outputs/sub').symlink_to(self.root / 'outputs', target_is_directory=True)
        with self.assertRaises(OSError):
            prepare_publication(self.plan, dict(files=['outputs/sub/a.txt']), self.root)

    def test_manifest_determinism_and_body_reference(self):
        p = self.publication
        self.assertEqual(p, prepare_publication(self.plan, self.manifest, self.root))
        self.assertTrue(p.title.startswith('[Codex] '))
        self.assertEqual(p.branch, 'research/codex-72-' + p.digest[:16])
        self.assertIn('https://github.com/' + REPOSITORY + '/issues/72', p.body)
        self.assertIn('<!-- c3-worker-pr-v1:' + p.digest + ' -->', p.body)
        (self.root / 'outputs/a.txt').write_bytes(b'different')
        self.assertNotEqual(p.digest, prepare_publication(self.plan, self.manifest, self.root).digest)

    def test_publish_draft_after_durable_intent_and_reinvoke_read_only(self):
        result = self.publish()
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual([path for path, _ in self.client.writes],
                         ['git/blobs', 'git/trees', 'git/commits', 'git/refs', 'pulls'])
        self.assertTrue(self.client.pr['draft'])
        self.assertEqual(self.client.writes[1][1]['base_tree'], BASE_TREE)
        self.assertEqual(self.client.writes[2][1]['parents'], [BASE])
        writes = list(self.client.writes)
        self.assertEqual(self.publish(), result)
        self.assertEqual(self.client.writes, writes)

    def test_wrong_authenticated_actor_or_moved_base_prevents_writes(self):
        self.client.actor = dict(login='ken2129', id=OWNER_ID)
        with self.assertRaises(ContractError):
            self.publish()
        self.assertFalse(self.intent.exists())
        self.client.actor = ACTOR
        self.client.base = 'e' * 40
        with self.assertRaises(ContractError):
            self.publish()
        self.assertEqual(self.client.writes, [])

    def test_every_uncertain_write_is_never_repeated(self):
        for failure in ['git/blobs', 'git/trees', 'git/commits', 'git/refs', 'pulls']:
            with self.subTest(failure=failure):
                self.intent = self.root / (failure.replace('/', '-') + '.json')
                self.client = FakeGitHub(self.publication, self.intent)
                self.client.fail = failure
                with self.assertRaises(GitHubFailure):
                    self.publish()
                before = list(self.client.writes)
                self.client.fail = None
                self.assertEqual(self.publish()['status'], 'RECONCILIATION_REQUIRED')
                self.assertEqual(self.client.writes, before)

    def test_pr_success_lost_response_is_reconciled_read_only(self):
        self.client.fail = 'pulls'
        self.client.fail_after_effect = True
        with self.assertRaises(GitHubFailure):
            self.publish()
        before = list(self.client.writes)
        self.assertEqual(self.publish()['status'], 'PUBLISHED')
        self.assertEqual(self.client.writes, before)

    def test_existing_branch_without_pr_does_not_create_pr(self):
        self.client.branch = HEAD
        self.assertEqual(self.publish()['status'], 'RECONCILIATION_REQUIRED')
        self.assertEqual(self.client.writes, [])
        self.assertFalse(self.intent.exists())

    def test_tampered_pr_or_tree_or_extra_diff_rejected_without_writes(self):
        self.publish()
        original = deepcopy(self.client.pr)
        before = list(self.client.writes)
        for change in [dict(body='altered'), dict(user=dict(login='ken2129', id=OWNER_ID)),
                       dict(draft=False), dict(html_url='https://example.com')]:
            self.client.pr = dict(original, **change)
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.publish()
        self.client.pr = original
        self.client.extra_diff = True
        with self.assertRaises(ContractError):
            self.publish()
        self.client.extra_diff = False
        self.client.tree_mode = '120000'
        with self.assertRaises(ContractError):
            self.publish()
        self.assertEqual(self.client.writes, before)

    def test_incomplete_or_foreign_local_intent_is_fail_closed(self):
        for content in [b'{', b'{}']:
            self.intent.write_bytes(content)
            with self.assertRaises(ContractError):
                self.publish()
            self.assertEqual(self.client.writes, [])

    def test_token_requires_private_worker_file_and_never_env_fallback(self):
        token_path = self.root / 'token'
        with patch('research_harness.worker_pr.WORKER_TOKEN_PATH', str(token_path)), \
                patch.dict(os.environ, {'GH_TOKEN': 'ignored' * 10, 'GITHUB_TOKEN': 'ignored' * 10}):
            with self.assertRaises(OSError):
                WorkerGitHub.from_worker_token()
            token_path.write_text('a' * 40)
            token_path.chmod(0o644)
            with self.assertRaises(ContractError):
                WorkerGitHub.from_worker_token()
            token_path.chmod(0o600)
            self.assertIsInstance(WorkerGitHub.from_worker_token(), WorkerGitHub)

    def test_rest_limits_no_retry_and_cli_sanitizes_errors(self):
        client = WorkerGitHub('x' * 40)
        with patch.object(client._opener, 'open', side_effect=URLError('SECRET')) as network:
            with self.assertRaisesRegex(GitHubFailure, 'outcome unavailable'):
                client.request('POST', f'/repos/{REPOSITORY}/git/blobs', {})
            self.assertEqual(network.call_count, 1)
        with self.assertRaises(ContractError):
            client.request('POST', '/repos/other/repo/pulls', {})
        with self.assertRaises(ContractError):
            client.request('POST', f'/repos/{REPOSITORY}/git/blobs', dict(content='a' * MAX_JSON_BYTES))
        client._requests = 50
        with self.assertRaises(GitHubFailure):
            client.request('GET', '/user')
        with patch('research_harness.worker_pr._json_file', side_effect=ContractError('SECRET')), \
                patch('sys.stderr') as stderr:
            self.assertEqual(main(['publish', '--approved-plan', 'plan.json', '--manifest', 'files.json',
                                   '--intent', str(self.intent)]), 2)
            self.assertNotIn('SECRET', str(stderr.write.call_args_list))


if __name__ == '__main__':
    unittest.main()
