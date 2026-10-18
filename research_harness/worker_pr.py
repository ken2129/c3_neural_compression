"""Optional worker-run direct draft-PR helper; never called by the manager.

Configuration is disabled by default. A separately authorized installer must
provide a worker credential at WORKER_TOKEN_PATH. A non-owner identity is the
default; using the owner requires owner_identity_risk_accepted=True because the
worker could author owner approval comments. No gh config, environment token,
manager credential fallback, or automatic credential installation is used here.
The three configuration attestations are operator acknowledgments,
not proof of GitHub permissions or technical branch-level token restrictions.
The credential itself remains usable by worker code: this helper is a publishing
convenience, not a security boundary or an approval verifier.

Run ``python -m research_harness.worker_pr publish --approved-plan plan.json
--manifest files.json --intent /work/worker-home/pr-intent.json`` inside the
worker. The approved plan carries write_scope and direct_pr; the manifest is
{"files": ["relative/file", ...]}. Only listed regular files are uploaded.
A durable intent precedes every remote mutation. Any later invocation is
read-only reconciliation, even if no branch/PR exists. An uncertain publication
requires operator inspection; this helper never blindly retries remote writes.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .schema import ContractError, REPOSITORY, canonical, fields, loads, relative_path
from .store import SingleInstance

SCHEMA = 'c3-worker-pr-v1'
CONTROL_CONFIG_PATH = '/control/worker-pr.json'
CONTROL_TOKEN_PATH = '/control/worker-github-token'
WORKER_CONFIG_PATH = '/work/worker-home/worker-pr.json'
WORKER_TOKEN_PATH = '/work/worker-home/github-token'
OWNER_LOGIN = 'ken2129'
OWNER_ID = 55587773
CONSENTS = ('credential_access_approved', 'repository_scope_confirmed',
            'protected_branches_confirmed')
MAX_FILES = 32
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r'[0-9a-f]{40}\Z')


def default_worker_pr_config():
    return dict(schema=SCHEMA, enabled=False, repository=REPOSITORY,
                actor_login=None, actor_id=None, owner_identity_risk_accepted=False,
                **dict.fromkeys(CONSENTS, False))


def validate_worker_pr_config(value):
    """Validate intent only; this does not install credentials or grant access."""
    if value is None:
        return default_worker_pr_config()
    fields(value, ('schema',), ('enabled', 'repository', 'actor_login', 'actor_id',
                              'owner_identity_risk_accepted', *CONSENTS))
    result = {**default_worker_pr_config(), **value}
    if result['schema'] != SCHEMA or result['repository'] != REPOSITORY:
        raise ContractError('worker PR schema/repository mismatch')
    if any(type(result[key]) is not bool for key in ('enabled', 'owner_identity_risk_accepted', *CONSENTS)):
        raise ContractError('worker PR policy flags must be booleans')
    login, actor_id = result['actor_login'], result['actor_id']
    if login is not None and (type(login) is not str or
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?', login) is None):
        raise ContractError('invalid dedicated worker login')
    if actor_id is not None and (type(actor_id) is not int or actor_id <= 0):
        raise ContractError('invalid dedicated worker actor ID')
    if result['enabled']:
        if login is None or actor_id is None or not all(result[key] for key in CONSENTS):
            raise ContractError('explicit dedicated worker authorization required')
        owner_login = login.lower() == OWNER_LOGIN
        owner_id = actor_id == OWNER_ID
        if owner_login != owner_id:
            raise ContractError('owner login and actor ID must match')
        if owner_login and not result['owner_identity_risk_accepted']:
            raise ContractError('owner approval impersonation risk must be explicitly accepted')
    return result


def _sha(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ContractError('fixed Git commit/blob SHA required')
    return value


def _safe_path(value):
    relative_path(value)
    if (len(value.encode('utf-8')) > 256 or
            any(part.startswith('.') for part in value.split('/')) or
            any(char in value for char in '*?[]')):
        raise ContractError('unsafe publication path')
    return value


def _read_file(root, relative, limit):
    """Open beneath root without following symlinks, including intermediate ones."""
    _safe_path(relative)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = relative.split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise ContractError('bounded regular publication file required')
            content = source.read(limit + 1)
            if len(content) > limit:
                raise ContractError('publication file exceeds limit')
            return content, '100755' if info.st_mode & 0o111 else '100644'
    finally:
        os.close(directory)


@dataclass(frozen=True)
class Publication:
    issue: int
    base_branch: str
    base_sha: str
    title: str
    digest: str
    branch: str
    body: str
    files: tuple


def prepare_publication(plan, manifest, work_root):
    """Caller supplies an already-approved plan; untrusted JSON is not approval."""
    if type(plan) is not dict or 'write_scope' not in plan or 'direct_pr' not in plan:
        raise ContractError('approved plan with publication policy required')
    policy = plan['direct_pr']
    fields(policy, ('repository', 'issue_number', 'base_branch', 'base_sha',
                    'title', 'public_results_approved'))
    if (policy['repository'] != REPOSITORY or policy['base_branch'] != 'main' or
            policy['public_results_approved'] is not True):
        raise ContractError('approved fixed repository/base/publication required')
    issue = policy['issue_number']
    if type(issue) is not int or not 1 <= issue < 2 ** 31:
        raise ContractError('positive Issue number required')
    _sha(policy['base_sha'])
    title = policy['title']
    if (type(title) is not str or not title.strip() or len(title.encode('utf-8')) > 200
            or any(ord(char) < 32 for char in title)):
        raise ContractError('bounded PR title required')
    scope = plan['write_scope']
    if type(scope) is not list or not 1 <= len(scope) <= 8:
        raise ContractError('bounded approved write scope required')
    for path in scope:
        _safe_path(path)
    fields(manifest, ('files',))
    paths = manifest['files']
    if type(paths) is not list or not 1 <= len(paths) <= MAX_FILES:
        raise ContractError('bounded file manifest required')
    files, seen, total = [], set(), 0
    for path in paths:
        _safe_path(path)
        if path in seen or not any(path == allowed or path.startswith(allowed + '/') for allowed in scope):
            raise ContractError('duplicate or out-of-scope publication file')
        seen.add(path)
        content, mode = _read_file(work_root, path, MAX_FILE_BYTES)
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ContractError('publication aggregate byte limit exceeded')
        blob_sha = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        files.append((path, mode, content, blob_sha))
    files.sort()
    identity = dict(plan=plan, files=[dict(path=p, mode=m, sha=s) for p, m, _, s in files])
    if len(canonical(identity)) > MAX_JSON_BYTES:
        raise ContractError('publication plan exceeds limit')
    digest = hashlib.sha256(canonical(identity)).hexdigest()
    branch = f'research/codex-{issue}-{digest[:16]}'
    title = title if title.startswith('[Codex] ') else '[Codex] ' + title
    body = (f'Issue: https://github.com/{REPOSITORY}/issues/{issue}\n\n'
            f'<!-- c3-worker-pr-v1:{digest} -->\n')
    return Publication(issue, 'main', policy['base_sha'], title, digest, branch, body, tuple(files))


class GitHubFailure(ContractError):
    """Sanitized failure; response bodies and credentials must never be printed."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise GitHubFailure('GitHub redirect refused')


class WorkerGitHub:
    """Fixed-origin, fixed-repository REST client, with no retries or auth fallback."""
    def __init__(self, token):
        if type(token) is not str or re.fullmatch(r'[A-Za-z0-9_]{20,4096}', token) is None:
            raise ContractError('invalid dedicated worker token')
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._deadline = time.monotonic() + 120
        self._requests = 0

    @classmethod
    def from_worker_token(cls):
        fd = os.open(WORKER_TOKEN_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4097):
                raise ContractError('private worker-owned token file required')
            try:
                token = source.read(4098).decode('ascii').strip()
            except UnicodeError as exc:
                raise ContractError('invalid worker token encoding') from exc
        return cls(token)

    def request(self, method, path, payload=None, *, missing_ok=False):
        if method not in ('GET', 'POST') or not (path == '/user' or path.startswith(f'/repos/{REPOSITORY}/')):
            raise ContractError('unexpected worker GitHub endpoint')
        remaining = self._deadline - time.monotonic()
        self._requests += 1
        if remaining <= 0 or self._requests > 50:
            raise GitHubFailure('GitHub publication read/write budget exhausted')
        data = None if payload is None else canonical(payload)
        if data is not None and len(data) > MAX_JSON_BYTES:
            raise ContractError('GitHub request exceeds limit')
        request = Request('https://api.github.com' + path, data=data, method=method,
            headers={'Authorization': 'Bearer ' + self._token,
                     'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                     'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'c3-worker-pr-v1'})
        try:
            with self._opener.open(request, timeout=min(15, remaining)) as response:
                raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES or time.monotonic() > self._deadline:
                raise GitHubFailure('GitHub response exceeds budget')
            return loads(raw)
        except HTTPError as exc:
            if missing_ok and method == 'GET' and exc.code == 404:
                return None
            raise GitHubFailure(f'GitHub request failed (HTTP {exc.code})') from None
        except (URLError, TimeoutError, OSError, ValueError, RecursionError):
            raise GitHubFailure('GitHub request outcome unavailable') from None


def _endpoint(suffix):
    return f'/repos/{REPOSITORY}/' + suffix


def _actor_matches(actor, config):
    return (type(actor) is dict and type(actor.get('id')) is int and
            actor['id'] == config['actor_id'] and
            type(actor.get('login')) is str and actor['login'].lower() == config['actor_login'].lower())


def _reconcile(client, publication, config):
    """Only reads; incomplete/mismatched publication must never trigger a retry."""
    p = publication
    ref = client.request('GET', _endpoint('git/ref/heads/' + quote(p.branch, safe='/')), missing_ok=True)
    prs = client.request('GET', _endpoint('pulls?' + urlencode(dict(
        state='all', head='ken2129:' + p.branch, base=p.base_branch, per_page=100))))
    if type(prs) is not list or len(prs) >= 100:
        raise ContractError('ambiguous PR lookup')
    if ref is None:
        if prs:
            raise ContractError('PR has no matching branch')
        return None
    if (type(ref) is not dict or ref.get('ref') != 'refs/heads/' + p.branch or
            type(ref.get('object')) is not dict or ref['object'].get('type') != 'commit'):
        raise ContractError('unexpected branch identity')
    head = _sha(ref['object'].get('sha'))
    commit = client.request('GET', _endpoint('git/commits/' + head))
    if (type(commit) is not dict or commit.get('sha') != head or
            commit.get('message') != p.title + '\n\n' + p.body or
            type(commit.get('parents')) is not list or len(commit['parents']) != 1 or
            commit['parents'][0].get('sha') != p.base_sha):
        raise ContractError('publication commit does not match intent')
    comparison = client.request('GET', _endpoint('compare/' + p.base_sha + '...' + head))
    if (type(comparison) is not dict or comparison.get('total_commits') != 1 or
            type(comparison.get('files')) is not list or len(comparison['files']) > MAX_FILES):
        raise ContractError('publication diff exceeds intent')
    expected = {path: sha for path, _, _, sha in p.files}
    actual = {}
    for change in comparison['files']:
        if (type(change) is not dict or change.get('status') not in ('added', 'modified') or
                change.get('filename') in actual):
            raise ContractError('unexpected publication diff')
        actual[change.get('filename')] = change.get('sha')
    # Unchanged listed files do not appear in a GitHub comparison. All changed
    # files must match; check every listed blob/mode against the resulting tree.
    if not actual or any(path not in expected or expected[path] != sha for path, sha in actual.items()):
        raise ContractError('publication diff differs from manifest')
    tree_sha = _sha(commit.get('tree', {}).get('sha'))
    tree = client.request('GET', _endpoint('git/trees/' + tree_sha + '?recursive=1'))
    if type(tree) is not dict or tree.get('truncated') is not False or type(tree.get('tree')) is not list:
        raise ContractError('complete publication tree unavailable')
    entries = {entry.get('path'): entry for entry in tree['tree'] if type(entry) is dict}
    for path, mode, _, sha in p.files:
        entry = entries.get(path, {})
        if entry.get('type') != 'blob' or entry.get('mode') != mode or entry.get('sha') != sha:
            raise ContractError('publication tree differs from manifest')
    if not prs:
        return dict(status='RECONCILIATION_REQUIRED', branch=p.branch, reason='branch_exists_without_pr')
    if len(prs) != 1:
        raise ContractError('multiple PRs for publication branch')
    pr = prs[0]
    if type(pr) is not dict:
        raise ContractError('invalid PR metadata')
    number = pr.get('number')
    if (type(number) is not int or number <= 0 or not _actor_matches(pr.get('user'), config) or
            pr.get('title') != p.title or pr.get('body') != p.body or pr.get('draft') is not True or
            pr.get('head', {}).get('ref') != p.branch or pr['head'].get('sha') != head or
            pr['head'].get('repo', {}).get('full_name') != REPOSITORY or
            pr.get('base', {}).get('ref') != p.base_branch or
            pr['base'].get('repo', {}).get('full_name') != REPOSITORY or
            pr.get('html_url') != f'https://github.com/{REPOSITORY}/pull/{number}'):
        raise ContractError('PR does not match publication intent')
    return dict(status='PUBLISHED', branch=p.branch, pr_number=number, url=pr['html_url'])


def _intent(path, publication):
    """O_EXCL claim, fsync bytes and parent directory before first remote write."""
    value = dict(schema=SCHEMA, digest=publication.digest, branch=publication.branch,
                 base_sha=publication.base_sha)
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raw, _ = _read_file(path.parent, path.name, 4096)
        if loads(raw) != value:
            raise ContractError('existing publication intent does not match')
        return False
    with os.fdopen(fd, 'wb') as target:
        target.write(canonical(value) + b'\n')
        target.flush()
        os.fsync(target.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def publish(config, approved_plan, manifest, work_root, intent_path, *, client=None):
    config = validate_worker_pr_config(config)
    if not config['enabled']:
        raise ContractError('worker PR publishing is disabled')
    p = prepare_publication(approved_plan, manifest, work_root)
    client = WorkerGitHub.from_worker_token() if client is None else client
    with SingleInstance(str(intent_path) + '.lock'):
        if not _actor_matches(client.request('GET', '/user'), config):
            raise ContractError('credential is not the configured dedicated worker')
        existing = _reconcile(client, p, config)
        if existing is not None:
            return existing
        if not _intent(intent_path, p):
            return dict(status='RECONCILIATION_REQUIRED', branch=p.branch,
                        reason='prior_intent_without_visible_publication')
        base = client.request('GET', _endpoint('git/ref/heads/' + p.base_branch))
        if (type(base) is not dict or base.get('ref') != 'refs/heads/' + p.base_branch or
                base.get('object', {}).get('type') != 'commit' or
                base['object'].get('sha') != p.base_sha):
            raise ContractError('approved base branch moved')
        commit = client.request('GET', _endpoint('git/commits/' + p.base_sha))
        if type(commit) is not dict or commit.get('sha') != p.base_sha:
            raise ContractError('approved base commit identity mismatch')
        base_tree = _sha(commit.get('tree', {}).get('sha'))
        tree_entries = []
        for path, mode, content, sha in p.files:
            blob = client.request('POST', _endpoint('git/blobs'),
                dict(content=base64.b64encode(content).decode('ascii'), encoding='base64'))
            if blob.get('sha') != sha:
                raise ContractError('GitHub blob identity mismatch')
            tree_entries.append(dict(path=path, mode=mode, type='blob', sha=sha))
        tree = client.request('POST', _endpoint('git/trees'), dict(base_tree=base_tree, tree=tree_entries))
        tree_sha = _sha(tree.get('sha'))
        if tree_sha == base_tree:
            raise ContractError('manifest makes no changes to approved base')
        commit = client.request('POST', _endpoint('git/commits'),
            dict(message=p.title + '\n\n' + p.body, tree=tree_sha, parents=[p.base_sha]))
        head = _sha(commit.get('sha'))
        client.request('POST', _endpoint('git/refs'), dict(ref='refs/heads/' + p.branch, sha=head))
        client.request('POST', _endpoint('pulls'),
            dict(title=p.title, body=p.body, head=p.branch, base=p.base_branch, draft=True))
        result = _reconcile(client, p, config)
        if result is None or result['status'] != 'PUBLISHED':
            raise GitHubFailure('publication response needs read-only reconciliation')
        return result


def _json_file(path):
    raw, _ = _read_file(Path(path).parent, Path(path).name, MAX_JSON_BYTES)
    return loads(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('publish',))
    parser.add_argument('--config', default=WORKER_CONFIG_PATH)
    parser.add_argument('--approved-plan', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--work-root', default='/work/goal')
    parser.add_argument('--intent', required=True)
    args = parser.parse_args(argv)
    try:
        result = publish(_json_file(args.config), _json_file(args.approved_plan),
                         _json_file(args.manifest), args.work_root, args.intent)
        print(canonical(result).decode())
        return 0 if result['status'] == 'PUBLISHED' else 2
    except (ContractError, OSError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        # Never echo arbitrary exception bodies, local file contents, or tokens.
        print('Worker PR not confirmed; preserve the intent and inspect with read-only reconciliation.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
