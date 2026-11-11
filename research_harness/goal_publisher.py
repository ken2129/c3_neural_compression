"""Manager-only, exact reviewed goal diffs via Git data; no code execution.

Fresh authorize_callback MUST verify current owner approval and a separate
content-review receipt bound to request.digest, candidate.candidate_digest, all
paths and exact output bytes. Execution approval alone must return False. The
manager must have stopped/revoked worker access before obtaining credentials.
Never persist raw credentials or use model/Issue strings as GitHub routes.

Journal each intent durably before writing. TREE_UNKNOWN and COMMIT_UNKNOWN
without a returned object SHA deliberately remain unresolved on restart: absence
is not permission to replay. Branch/PR uncertainty supports exact readback only.
"""
from dataclasses import asdict, dataclass, replace
import hashlib
import math
import re
from urllib.parse import urlencode

from .goal_artifacts import HARD_MAX_BYTES
from .goal_result import GoalPublicationRequest
from .result_publisher import GhResultTransport, RemoteConflict, TransportUnknown, TRANSPORT_REASONS
from .schema import ContractError, REPOSITORY, canonical, fields, loads

SHA = r'[0-9a-f]{40}'
BRANCH = r'research-goal-i[1-9][0-9]{0,9}-[0-9a-f]{32}'


def _sha(value):
    if type(value) is not str or not re.fullmatch(SHA, value):
        raise RemoteConflict('invalid goal Git object identity')
    return value


def _blob(content):
    return hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()


def _files(request):
    if type(request) is not GoalPublicationRequest:
        raise ContractError('exact goal publication request required')
    changes = []
    for change in request.result.candidate.changes:
        entries = (change.before, change.after)
        if all(e is None or e.kind == 'directory' for e in entries):
            continue  # Git does not store empty directories or directory modes.
        if any(e is not None and (e.kind != 'file' or e.mode not in (0o644, 0o755)) for e in entries):
            raise ContractError('goal publication requires ordinary Git file modes')
        changes.append(change)
    if not changes or len(changes) > 100:
        raise ContractError('goal publication requires 1 to 100 changed files')
    return changes


@dataclass(frozen=True)
class GoalPublicationState:
    request_digest: str
    branch: str
    phase: str = 'NEW'
    tree_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    reason: str | None = None

    def __post_init__(self):
        if (type(self.request_digest) is not str or not re.fullmatch('[0-9a-f]{64}', self.request_digest)
                or type(self.branch) is not str or not re.fullmatch(BRANCH, self.branch)):
            raise ContractError('invalid goal publication journal binding')
        if self.phase not in ('NEW', 'TREE_UNKNOWN', 'TREE_READY', 'COMMIT_UNKNOWN',
                              'COMMIT_READY', 'BRANCH_UNKNOWN', 'BRANCH_READY', 'PR_UNKNOWN',
                              'PUBLISHED', 'BLOCKED'):
            raise ContractError('invalid goal publication phase')
        for value in (self.tree_sha, self.head_sha):
            if value is not None:
                _sha(value)
        if self.phase in ('TREE_READY', 'COMMIT_UNKNOWN', 'COMMIT_READY', 'BRANCH_UNKNOWN',
                          'BRANCH_READY', 'PR_UNKNOWN', 'PUBLISHED') and self.tree_sha is None:
            raise ContractError('goal publication tree missing')
        if self.phase in ('COMMIT_READY', 'BRANCH_UNKNOWN', 'BRANCH_READY', 'PR_UNKNOWN', 'PUBLISHED') and self.head_sha is None:
            raise ContractError('goal publication head missing')
        if self.pr_number is not None and (type(self.pr_number) is not int or self.pr_number <= 0):
            raise ContractError('invalid goal PR number')
        if self.phase == 'PUBLISHED' and self.pr_number is None:
            raise ContractError('goal publication PR missing')
        if self.reason not in (None, 'AUTHORIZATION_MISSING', 'REMOTE_CONFLICT', 'BASE_CHANGED') and self.reason not in TRANSPORT_REASONS:
            raise ContractError('invalid goal publication reason')

    @property
    def url(self):
        return None if self.phase != 'PUBLISHED' else f'https://github.com/{REPOSITORY}/pull/{self.pr_number}'

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        fields(value, cls.__dataclass_fields__)
        return cls(**value)


class GhGoalTransport(GhResultTransport):
    """Reuse bounded trusted gh execution, with a separate strict route allowlist."""
    def _request(self, method, suffix, payload=None, *, absent=False):
        if not self.enabled:
            raise ContractError('live goal publication disabled')
        routes = {
            'GET': (rf'/git/ref/heads/(?:main|{BRANCH})', rf'/git/commits/{SHA}',
                    rf'/git/trees/{SHA}\?recursive=1', r'/pulls/[1-9][0-9]{0,9}',
                    rf'/pulls\?state=all&head=ken2129%3A{BRANCH}&base=main&per_page=100&page=1'),
            'POST': (r'/git/trees', r'/git/commits', r'/git/refs', r'/pulls'),
        }
        if method not in routes or type(suffix) is not str or not any(re.fullmatch(p, suffix) for p in routes[method]):
            raise ContractError('goal GitHub route is not allowlisted')
        encoded = b'' if payload is None else canonical(payload)
        if len(encoded) > HARD_MAX_BYTES * 6 + 1024 * 1024:
            raise ContractError('goal publication payload exceeds bound')
        command = [self._executable(), 'api', '--hostname', 'github.com', '--method', method,
                   '--include', '-H', 'Accept: application/vnd.github+json',
                   '-H', 'X-GitHub-Api-Version: 2022-11-28', 'repos/' + REPOSITORY + suffix]
        if payload is not None:
            command += ['--input', '-']
        code, raw = self._run(command, encoded)
        try:
            if type(raw) is not bytes or not raw or len(raw) > 2 * 1024 * 1024:
                raise ValueError()
            separator = b'\r\n\r\n' if b'\r\n\r\n' in raw else b'\n\n'
            header, body = raw.split(separator, 1)
            match = re.fullmatch(rb'HTTP/[0-9.]+ ([1-5][0-9]{2})(?: [^\r\n]*)?', header.splitlines()[0])
            if len(header) > 16384 or match is None:
                raise ValueError()
            status = int(match.group(1))
        except (ValueError, IndexError):
            raise TransportUnknown(reason='MALFORMED_RESPONSE') from None
        if absent and method == 'GET' and status == 404:
            return None
        if not 200 <= status < 300:
            reason = 'HTTP_' + str(status) if status in (401, 403, 422, 429) or 500 <= status <= 599 else 'HTTP_OTHER'
            raise TransportUnknown(reason=reason)
        if code != 0:
            raise TransportUnknown(reason='TRANSPORT_UNAVAILABLE')
        try:
            return loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise TransportUnknown(reason='MALFORMED_RESPONSE') from None

    def _ref(self, branch, *, absent):
        if branch != 'main' and not re.fullmatch(BRANCH, branch):
            raise ContractError('invalid goal branch')
        data = self._request('GET', '/git/ref/heads/' + branch, absent=absent)
        if data is None:
            return None
        try:
            if data['ref'] != 'refs/heads/' + branch or data['object']['type'] != 'commit':
                raise RemoteConflict('goal branch identity mismatch')
            return _sha(data['object']['sha'])
        except (KeyError, TypeError):
            raise RemoteConflict('goal branch cannot be verified') from None

    def branch_sha(self, request):
        _files(request)
        return self._ref(request.branch, absent=True)

    def _commit(self, sha):
        data = self._request('GET', '/git/commits/' + _sha(sha))
        try:
            if data['sha'] != sha:
                raise RemoteConflict('goal commit identity mismatch')
            _sha(data['tree']['sha'])
            return data
        except (KeyError, TypeError):
            raise RemoteConflict('goal commit cannot be verified') from None

    def _tree(self, sha):
        data = self._request('GET', '/git/trees/' + _sha(sha) + '?recursive=1')
        try:
            if data['sha'] != sha or data['truncated'] is not False or type(data['tree']) is not list:
                raise RemoteConflict('goal tree incomplete')
            result = {}
            for item in data['tree']:
                if item['type'] == 'tree':
                    continue
                path = item['path']
                if path in result or item['type'] not in ('blob', 'commit'):
                    raise RemoteConflict('invalid goal tree entries')
                result[path] = (item['mode'], item['type'], _sha(item['sha']))
            return result
        except (KeyError, TypeError):
            raise RemoteConflict('goal tree cannot be verified') from None

    def _expected(self, request):
        base = self._commit(request.base_sha)
        expected = self._tree(base['tree']['sha'])
        for change in _files(request):
            before = change.before
            original = None if before is None else ('100755' if before.mode == 0o755 else '100644', 'blob', _blob(before.content))
            if expected.get(change.path) != original:
                raise RemoteConflict('goal source content differs from publication base')
            if change.after is None:
                expected.pop(change.path)
            else:
                after = change.after
                expected[change.path] = ('100755' if after.mode == 0o755 else '100644', 'blob', _blob(after.content))
        return base['tree']['sha'], expected

    def create_tree(self, request):
        base_tree, _ = self._expected(request)
        entries = []
        for change in _files(request):
            entry = dict(path=change.path, mode='100755' if (change.after or change.before).mode == 0o755 else '100644', type='blob')
            if change.after is None:
                entry['sha'] = None
            else:
                entry['content'] = change.after.content.decode('utf-8')
            entries.append(entry)
        data = self._request('POST', '/git/trees', dict(base_tree=base_tree, tree=entries))
        try:
            return _sha(data['sha'])
        except (KeyError, TypeError):
            raise TransportUnknown(reason='MALFORMED_RESPONSE') from None

    def verify_tree(self, request, tree):
        _, expected = self._expected(request)
        if self._tree(tree) != expected:
            raise RemoteConflict('goal tree differs from exact reviewed files')

    def create_commit(self, request, tree):
        data = self._request('POST', '/git/commits', dict(message='Reviewed goal changes ' + request.digest,
                             tree=_sha(tree), parents=[request.base_sha]))
        try:
            return _sha(data['sha'])
        except (KeyError, TypeError):
            raise TransportUnknown(reason='MALFORMED_RESPONSE') from None

    def verify_commit(self, request, head):
        data = self._commit(head)
        parents = data.get('parents')
        if (type(parents) is not list or len(parents) != 1
                or type(parents[0]) is not dict or parents[0].get('sha') != request.base_sha):
            raise RemoteConflict('goal commit parent mismatch')
        self.verify_tree(request, data['tree']['sha'])

    def commit_tree(self, head):
        return self._commit(head)['tree']['sha']

    def create_branch(self, request, head):
        self._request('POST', '/git/refs', dict(ref='refs/heads/' + request.branch, sha=_sha(head)))

    def find_pr(self, request):
        query = urlencode(dict(state='all', head='ken2129:' + request.branch, base='main', per_page=100, page=1))
        data = self._request('GET', '/pulls?' + query)
        if type(data) is not list or len(data) > 1:
            raise RemoteConflict('goal PR identity ambiguous')
        if not data:
            return None
        number = data[0].get('number')
        if type(number) is not int or number <= 0:
            raise RemoteConflict('goal PR number invalid')
        return self._request('GET', '/pulls/' + str(number))

    def create_pr(self, request):
        self._request('POST', '/pulls', dict(title=request.title, body=request.body,
                                           head=request.branch, base='main', draft=True))


def matching_goal_publication_receipt(request, comments, now):
    """Authenticate a fresh exact-content approval; execution receipts never count."""
    from .owner_approval import OWNER_ID, OWNER_LOGIN, MissingApproval, RevokedApproval
    from .issue_intake import timestamp
    _files(request)
    if (type(comments) is not list or type(now) not in (int, float)
            or not math.isfinite(now)):
        raise MissingApproval('goal content approval unavailable')
    matches = []
    invalid = False
    expected = dict(repository=REPOSITORY, issue=request.result.issue_number,
                    request_digest=request.digest, base_sha=request.base_sha,
                    candidate_digest=request.candidate_digest,
                    issue_content_sha256=request.issue_content_sha256,
                    source_sha=request.result.source_sha)
    for comment in comments:
        if type(comment) is not dict:
            raise MissingApproval('goal content approval metadata invalid')
        author = comment.get('author')
        if (type(author) is not dict or type(author.get('databaseId')) is not int
                or author.get('databaseId') != OWNER_ID or author.get('login') != OWNER_LOGIN):
            continue
        try:
            body = loads(comment['body'])
        except (ContractError, TypeError, KeyError, RecursionError):
            continue
        if type(body) is not dict or body.get('schema') != 'c3-goal-publication-approval-v1' or body.get('request_digest') != request.digest:
            continue
        try:
            fields(body, ('schema', *expected.keys(), 'expires_at', 'decision'))
            if (any(body[k] != v for k, v in expected.items()) or type(body['issue']) is not int
                    or type(comment['id']) is not str or not comment['id'] or len(comment['id']) > 200
                    or comment['createdAt'] != comment['updatedAt']
                    or comment['lastEditedAt'] is not None or comment['editor'] is not None
                    or comment['isMinimized'] is not False):
                raise ContractError('goal content approval mismatch')
            created = timestamp(comment['createdAt'])
            if created > now or timestamp(body['expires_at']) <= now:
                raise ContractError('goal content approval expired or future dated')
            if body['decision'] == 'cancel-publication':
                raise RevokedApproval('goal publication approval revoked')
            if body['decision'] != 'approve-publication':
                raise ContractError('not a content publication approval')
            matches.append(body)
        except RevokedApproval:
            raise
        except (ContractError, KeyError, TypeError, ValueError):
            invalid = True
    if invalid or len(matches) != 1:
        raise MissingApproval('one current unedited exact-content approval required')
    return matches[0]


class GitHubGoalPublisher:
    def __init__(self, transport, *, persist_state):
        if not callable(persist_state):
            raise ContractError('durable goal publication journal required')
        self.transport = transport
        self.persist_state = persist_state

    @staticmethod
    def _verify_pr(request, state, pr):
        try:
            number = pr['number']
            if (type(number) is not int or number <= 0 or pr['state'] != 'open'
                    or pr['draft'] is not True or pr.get('merged') is not False
                    or pr['head']['ref'] != request.branch or pr['head']['sha'] != state.head_sha
                    or pr['head']['repo']['full_name'] != REPOSITORY
                    or pr['base']['ref'] != 'main' or pr['base']['sha'] != request.base_sha
                    or pr['base']['repo']['full_name'] != REPOSITORY
                    or pr['title'] != request.title or pr['body'] != request.body
                    or pr['html_url'] != f'https://github.com/{REPOSITORY}/pull/{number}'):
                raise RemoteConflict('goal PR identity mismatch')
            return number
        except (KeyError, TypeError):
            raise RemoteConflict('goal PR cannot be verified') from None

    def publish(self, request, state=None, *, authorized=False, authorize_callback=None):
        _files(request)
        if state is None:
            state = GoalPublicationState(request.digest, request.branch)
        if (type(state) is not GoalPublicationState or state.request_digest != request.digest
                or state.branch != request.branch):
            raise ContractError('goal journal binding mismatch')

        def save(**changes):
            nonlocal state
            if 'phase' in changes:
                changes.setdefault('reason', None)
            updated = replace(state, **changes)
            self.persist_state(updated)
            state = updated

        def write(phase, method, *args):
            try:
                allowed = authorized is True and callable(authorize_callback) and authorize_callback() is True
            except Exception:
                allowed = False
            if not allowed:
                save(reason='AUTHORIZATION_MISSING')
                return None
            if self.transport.base_sha() != request.base_sha:
                save(phase='BLOCKED', reason='BASE_CHANGED')
                return None
            save(phase=phase)
            return getattr(self.transport, method)(request, *args)

        try:
            if state.phase in ('BLOCKED', 'PUBLISHED', 'TREE_UNKNOWN', 'COMMIT_UNKNOWN'):
                return state
            if state.phase == 'NEW':
                if self.transport.base_sha() != request.base_sha:
                    save(phase='BLOCKED', reason='BASE_CHANGED')
                    return state
                existing = self.transport.branch_sha(request)
                if existing is not None:
                    self.transport.verify_commit(request, existing)
                    tree = self.transport.commit_tree(existing)
                    save(phase='BRANCH_READY', tree_sha=_sha(tree), head_sha=existing)
                else:
                    tree = write('TREE_UNKNOWN', 'create_tree')
                    if state.phase == 'BLOCKED' or state.reason == 'AUTHORIZATION_MISSING':
                        return state
                    _sha(tree)
                    self.transport.verify_tree(request, tree)
                    save(phase='TREE_READY', tree_sha=tree)
            if state.phase == 'TREE_READY':
                self.transport.verify_tree(request, state.tree_sha)
                head = write('COMMIT_UNKNOWN', 'create_commit', state.tree_sha)
                if state.phase == 'BLOCKED' or state.reason == 'AUTHORIZATION_MISSING':
                    return state
                _sha(head)
                self.transport.verify_commit(request, head)
                save(phase='COMMIT_READY', head_sha=head)
            if state.phase == 'COMMIT_READY':
                self.transport.verify_commit(request, state.head_sha)
                existing = self.transport.branch_sha(request)
                if existing is None:
                    write('BRANCH_UNKNOWN', 'create_branch', state.head_sha)
                    if state.phase == 'BLOCKED' or state.reason == 'AUTHORIZATION_MISSING':
                        return state
                elif existing != state.head_sha:
                    raise RemoteConflict('goal branch changed')
                else:
                    save(phase='BRANCH_READY')
            if state.phase == 'BRANCH_UNKNOWN':
                existing = self.transport.branch_sha(request)
                if existing is None:
                    return state
                if existing != state.head_sha:
                    raise RemoteConflict('goal branch head mismatch')
                self.transport.verify_commit(request, state.head_sha)
                save(phase='BRANCH_READY')
            if state.phase == 'BRANCH_READY':
                if self.transport.branch_sha(request) != state.head_sha:
                    raise RemoteConflict('goal branch changed')
                self.transport.verify_commit(request, state.head_sha)
                pr = self.transport.find_pr(request)
                if pr is not None:
                    save(phase='PUBLISHED', pr_number=self._verify_pr(request, state, pr))
                    return state
                write('PR_UNKNOWN', 'create_pr')
                if state.phase == 'BLOCKED' or state.reason == 'AUTHORIZATION_MISSING':
                    return state
            if state.phase == 'PR_UNKNOWN':
                pr = self.transport.find_pr(request)
                if pr is None:
                    return state
                if self.transport.branch_sha(request) != state.head_sha:
                    raise RemoteConflict('goal branch changed after PR creation')
                self.transport.verify_commit(request, state.head_sha)
                save(phase='PUBLISHED', pr_number=self._verify_pr(request, state, pr))
            return state
        except TransportUnknown as exc:
            if state.reason is None and exc.reason is not None:
                save(reason=exc.reason)
            return state
        except RemoteConflict:
            save(phase='BLOCKED', reason='REMOTE_CONFLICT')
            return state
