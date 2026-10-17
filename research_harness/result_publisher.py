"""Bounded, manager-only publication of validated CPU-canary metadata.

This adapter is not a credential/sandbox boundary. The trusted controller must
verify isolation, current owner approval and runtime cost limits before enabling
it. Issue text cannot configure the transport. No source code, model prose,
credentials, merges, Issue closure or experiment execution is supported.

The journal callback must durably commit each state before returning, and the
controller must serialize publishers. Unknown writes are reconciled read-only;
absence after an uncertain write never authorizes a repeat write.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, replace
import hashlib
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
from urllib.parse import urlencode

from .schema import ContractError, REPOSITORY, canonical, fields, identifier, loads

SCHEMA = 'c3-cpu-canary-result-v1'
EXPECTED_CHECKSUM = sum(i * i for i in range(10000))
MAX_CONTENT = 16384
MAX_RESPONSE = 2 * 1024 * 1024
MAX_SECONDS = 20
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_PREFIX = f'repos/{REPOSITORY}'


def _sha(value, *, digest=False):
    if type(value) is not str or not (_DIGEST if digest else _SHA).fullmatch(value):
        raise ContractError('invalid publication digest')
    return value


def _int(value):
    if type(value) is not int or not 1 <= value <= 2 ** 31 - 1:
        raise ContractError('invalid publication number')
    return value


def _digest(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ValidatedCanaryResult:
    request_id: str
    run_id: str
    issue_number: int
    plan_digest: str
    source_sha: str
    outcome: str
    checksum: int | None
    elapsed_seconds: float
    artifact_sha256: str

    def __post_init__(self):
        identifier(self.request_id); identifier(self.run_id)
        _int(self.issue_number)
        _sha(self.plan_digest, digest=True); _sha(self.source_sha)
        _sha(self.artifact_sha256, digest=True)
        if self.issue_number == 56:
            raise ContractError('harness Issue is excluded from result publication')
        if self.outcome not in ('success', 'failure'):
            raise ContractError('unsupported canary outcome')
        if self.outcome == 'success':
            if type(self.checksum) is not int or self.checksum != EXPECTED_CHECKSUM:
                raise ContractError('canary checksum does not validate')
        elif self.checksum is not None:
            raise ContractError('failed canary must not claim a checksum')
        if (type(self.elapsed_seconds) not in (int, float)
                or not 0 <= self.elapsed_seconds <= 120
                or not math.isfinite(self.elapsed_seconds)):
            raise ContractError('canary elapsed time outside bounds')

    @classmethod
    def from_bytes(cls, raw, *, request_id, run_id, issue_number, plan_digest, source_sha):
        """Check a bounded closed artifact against independently supplied bindings.

        This proves schema/checksum identity, not that an isolated experiment ran.
        The controller remains responsible for its trusted execution receipt.
        """
        if type(raw) is not bytes or not raw or len(raw) > MAX_CONTENT:
            raise ContractError('canary artifact must be bounded bytes')
        try:
            data = loads(raw)
        except (UnicodeError, RecursionError, ValueError):
            raise ContractError('invalid canary artifact') from None
        fields(data, ('schema', 'request_id', 'run_id', 'issue_number', 'plan_digest',
                      'source_sha', 'outcome', 'checksum', 'elapsed_seconds'))
        expected = dict(request_id=request_id, run_id=run_id, issue_number=issue_number,
                        plan_digest=plan_digest, source_sha=source_sha)
        if data['schema'] != SCHEMA or any(data[key] != value for key, value in expected.items()):
            raise ContractError('canary artifact binding mismatch')
        return cls(**{key: data[key] for key in data if key != 'schema'}, artifact_sha256=_digest(raw))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        fields(value, cls.__dataclass_fields__)
        return cls(**value)


@dataclass(frozen=True)
class PublicationRequest:
    result: ValidatedCanaryResult
    base_sha: str
    issue_content_sha256: str

    def __post_init__(self):
        if type(self.result) is not ValidatedCanaryResult:
            raise ContractError('validated canary result required')
        _sha(self.base_sha); _sha(self.issue_content_sha256, digest=True)

    @property
    def payload(self):
        return dict(schema='c3-public-canary-result-v1', repository=REPOSITORY,
                    validation_scope='cpu_canary_checksum_only_no_scientific_claim',
                    base_sha=self.base_sha, issue_content_sha256=self.issue_content_sha256,
                    **self.result.to_dict())

    @property
    def content(self):
        return canonical(self.payload) + b'\n'

    @property
    def digest(self):
        return _digest(self.content)

    @property
    def branch(self):
        return f'research-results-i{self.result.issue_number}-{self.digest[:32]}'

    @property
    def path(self):
        return f'docs/research/generated/{self.result.run_id}.json'

    @property
    def title(self):
        return f'[研究結果] CPU カナリア実行結果: Issue #{self.result.issue_number}'

    @property
    def body(self):
        # No Issue description, model response, logs or private paths are copied.
        outcome = {'success': '成功', 'failure': '失敗'}[self.result.outcome]
        return (f'<!-- c3-result-publication:{self.digest} -->\n'
                f'関連 Issue: #{self.result.issue_number}\n\n'
                'CPU カナリアの実行結果です。科学的な圧縮性能の評価ではありません。\n'
                f'結果: {outcome}\n経過時間（秒）: {self.result.elapsed_seconds}\n'
                f'チェックサム: {self.result.checksum}\n'
                f'実行 ID: {self.result.run_id}\n'
                f'実行元コミット: {self.result.source_sha}\n'
                f'基準コミット: {self.base_sha}\n'
                f'成果物 SHA-256: {self.result.artifact_sha256}\n\n'
                '生成された結果 JSON のみを追加します。マージは人による確認が必要です。')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        fields(value, cls.__dataclass_fields__)
        return cls(ValidatedCanaryResult.from_dict(value['result']), value['base_sha'], value['issue_content_sha256'])


@dataclass(frozen=True)
class PublicationState:
    request_digest: str
    branch: str
    phase: str = 'NEW'
    head_sha: str | None = None
    pr_number: int | None = None
    reason: str | None = None

    def __post_init__(self):
        _sha(self.request_digest, digest=True)
        if type(self.branch) is not str or not re.fullmatch(r'research-results-i[1-9][0-9]{0,9}-[0-9a-f]{32}', self.branch):
            raise ContractError('invalid result branch')
        if self.phase not in ('NEW', 'BRANCH_UNKNOWN', 'BRANCH_READY', 'CONTENT_UNKNOWN',
                              'CONTENT_READY', 'PR_UNKNOWN', 'PUBLISHED', 'BLOCKED'):
            raise ContractError('invalid publication phase')
        if self.head_sha is not None:
            _sha(self.head_sha)
        if self.pr_number is not None:
            _int(self.pr_number)
        if self.phase in ('CONTENT_READY', 'PR_UNKNOWN', 'PUBLISHED') and self.head_sha is None:
            raise ContractError('published content identity missing')
        if self.phase == 'PUBLISHED' and self.pr_number is None:
            raise ContractError('published PR identity missing')
        if self.reason not in (None, 'AUTHORIZATION_MISSING', 'REMOTE_CONFLICT', 'BASE_CHANGED'):
            raise ContractError('unsupported publication reason')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        fields(value, cls.__dataclass_fields__)
        return cls(**value)

    @property
    def url(self):
        return None if self.pr_number is None else f'https://github.com/{REPOSITORY}/pull/{self.pr_number}'


class TransportUnknown(RuntimeError):
    """Request outcome unavailable; never contains remote text or credentials."""


class RemoteConflict(ContractError):
    pass


class GhResultTransport:
    """Fixed GitHub REST routes through already-installed gh, with no login.

    All three constructor flags must come from trusted manager configuration.
    They cannot establish OS isolation by themselves. This class never changes
    authentication, config, access, permissions, or branch protection settings.
    Tests should inject a fake transport into GitHubResultPublisher instead.
    """
    def __init__(self, *, enabled=False, manager_credentials_isolated=False, runtime_verified=False):
        self.enabled = enabled is True and manager_credentials_isolated is True and runtime_verified is True

    @staticmethod
    def _executable():
        for candidate in ('/usr/bin/gh', '/usr/local/bin/gh', '/bin/gh'):
            path = Path(candidate)
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if (stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
                    and not info.st_mode & 0o022 and os.access(path, os.X_OK)):
                return str(path)
        raise ContractError('trusted installed GitHub CLI unavailable')

    @staticmethod
    def _environment():
        # Inherit only existing manager authentication; no Issue-controlled env,
        # proxy URL, alternate host, debug trace, pager, git helper or executable.
        env = {key: os.environ[key] for key in
               ('HOME', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'GH_TOKEN', 'GITHUB_TOKEN') if key in os.environ}
        env.update(PATH='/usr/local/bin:/usr/bin:/bin', GH_HOST='github.com',
                   GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1',
                   GH_PAGER='cat', LC_ALL='C.UTF-8')
        return env

    @staticmethod
    def _run(command, payload):
        """Kill on deadline or aggregate output cap, without buffering unbounded logs."""
        process = None
        completed = False
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, shell=False, cwd='/',
                                       env=GhResultTransport._environment(), start_new_session=True)
            output, total, sent = bytearray(), 0, 0
            deadline = time.monotonic() + MAX_SECONDS
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                if payload:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                else:
                    process.stdin.close()
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TransportUnknown('GitHub request deadline exceeded')
                    for key, _ in selector.select(min(remaining, .1)):
                        if key.fileobj is process.stdin:
                            count = os.write(process.stdin.fileno(), payload[sent:sent + 4096])
                            sent += count
                            if sent == len(payload):
                                selector.unregister(process.stdin); process.stdin.close()
                            continue
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_RESPONSE:
                            raise TransportUnknown('GitHub response exceeded bound')
                        if key.fileobj is process.stdout:
                            output.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportUnknown('GitHub request deadline exceeded')
            code = process.wait(timeout=remaining)
            completed = True
            return code, bytes(output)
        except (OSError, subprocess.SubprocessError):
            raise TransportUnknown('GitHub request outcome unavailable') from None
        finally:
            if process is not None:
                if not completed:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def _request(self, method, suffix, payload=None, *, absent=False):
        if not self.enabled:
            raise ContractError('live result publication disabled by manager configuration')
        # Routes originate only in the typed methods below; reject URL/host tricks
        # even if an internal caller mistakenly passes a foreign path.
        if (method not in ('GET', 'POST', 'PUT') or type(suffix) is not str
                or not suffix.startswith('/') or '://' in suffix or '#' in suffix
                or '..' in suffix or any(ord(c) < 32 for c in suffix)):
            raise ContractError('invalid GitHub publication route')
        branch_pattern = r'research-results-i[1-9][0-9]{0,9}-[0-9a-f]{32}'
        path_pattern = r'docs/research/generated/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.json'
        allowed = {
            'GET': (rf'/git/ref/heads/(?:main|{branch_pattern})',
                    rf'/contents/{path_pattern}\?ref=[0-9a-f]{{40}}',
                    r'/commits/[0-9a-f]{40}', r'/pulls/[1-9][0-9]{0,9}',
                    rf'/pulls\?state=all&head=ken2129%3A{branch_pattern}&base=main&per_page=100&page=1'),
            'POST': (r'/git/refs', r'/pulls'),
            'PUT': (rf'/contents/{path_pattern}',),
        }
        if not any(re.fullmatch(pattern, suffix) for pattern in allowed[method]):
            raise ContractError('GitHub publication route is not allowlisted')
        endpoint = _PREFIX + suffix
        encoded = b'' if payload is None else canonical(payload)
        if len(encoded) > MAX_CONTENT * 2:
            raise ContractError('GitHub request exceeds bound')
        command = [self._executable(), 'api', '--hostname', 'github.com', '--method', method,
                   '--include', '-H', 'Accept: application/vnd.github+json',
                   '-H', 'X-GitHub-Api-Version: 2022-11-28', endpoint]
        if payload is not None:
            command += ['--input', '-']
        code, raw = self._run(command, encoded)
        try:
            separator = b'\r\n\r\n' if b'\r\n\r\n' in raw else b'\n\n'
            header, body = raw.split(separator, 1)
            match = re.fullmatch(rb'HTTP/[0-9.]+ ([1-5][0-9]{2})(?: [^\r\n]*)?', header.splitlines()[0])
            if match is None:
                raise ValueError()
            status = int(match.group(1))
            if absent and method == 'GET' and status == 404:
                return None
            if code or not 200 <= status < 300:
                raise ValueError()
            return loads(body)
        except (ValueError, IndexError, UnicodeError, RecursionError):
            raise TransportUnknown('GitHub response could not be verified') from None

    @staticmethod
    def _validate_request(request):
        if type(request) is not PublicationRequest:
            raise ContractError('strict publication request required')

    def base_sha(self):
        return self._ref('main', absent=False)

    def _ref(self, branch, *, absent):
        if branch != 'main' and not re.fullmatch(r'research-results-i[1-9][0-9]{0,9}-[0-9a-f]{32}', branch):
            raise ContractError('invalid result branch')
        data = self._request('GET', '/git/ref/heads/' + branch, absent=absent)
        if data is None:
            return None
        try:
            if data['ref'] != 'refs/heads/' + branch or data['object']['type'] != 'commit':
                raise RemoteConflict('unexpected result reference')
            return _sha(data['object']['sha'])
        except (KeyError, TypeError, ContractError):
            raise RemoteConflict('invalid result reference') from None

    def branch_sha(self, request):
        self._validate_request(request)
        return self._ref(request.branch, absent=True)

    def create_branch(self, request):
        self._validate_request(request)
        self._request('POST', '/git/refs', dict(ref='refs/heads/' + request.branch, sha=request.base_sha))

    def create_content(self, request):
        self._validate_request(request)
        self._request('PUT', '/contents/' + request.path,
                      dict(message=f'CPU canary result for Issue #{request.result.issue_number}',
                           content=base64.b64encode(request.content).decode('ascii'), branch=request.branch))

    def content_at(self, request, head):
        self._validate_request(request)
        _sha(head)
        data = self._request('GET', '/contents/' + request.path + '?ref=' + head, absent=True)
        if data is None:
            return None
        try:
            if (data['type'] != 'file' or data['path'] != request.path or data['encoding'] != 'base64'
                    or type(data['size']) is not int or not 0 <= data['size'] <= MAX_CONTENT
                    or type(data['content']) is not str or len(data['content']) > MAX_CONTENT * 2):
                raise ValueError()
            content = base64.b64decode(data['content'].replace('\n', ''), validate=True)
            if len(content) != data['size']:
                raise ValueError()
            return content
        except (KeyError, TypeError, ValueError):
            raise RemoteConflict('result content could not be verified') from None

    def verify_commit(self, request, head):
        self._validate_request(request)
        _sha(head)
        data = self._request('GET', '/commits/' + head)
        try:
            if (data['sha'] != head or len(data['parents']) != 1
                    or data['parents'][0]['sha'] != request.base_sha
                    or len(data['files']) != 1
                    or data['files'][0]['filename'] != request.path
                    or data['files'][0]['status'] != 'added'):
                raise RemoteConflict('result commit scope or parent mismatch')
        except (KeyError, TypeError, ContractError):
            raise RemoteConflict('result commit could not be verified') from None
        if self.content_at(request, head) != request.content:
            raise RemoteConflict('result artifact mismatch')

    def find_pr(self, request):
        self._validate_request(request)
        query = urlencode(dict(state='all', head='ken2129:' + request.branch,
                               base='main', per_page=100, page=1))
        data = self._request('GET', '/pulls?' + query)
        if type(data) is not list or len(data) > 1:
            raise RemoteConflict('result PR identity is ambiguous')
        if not data:
            return None
        try:
            number = _int(data[0]['number'])
        except (KeyError, TypeError, ContractError):
            raise RemoteConflict('result PR identity missing') from None
        return self._request('GET', '/pulls/' + str(number))

    def create_pr(self, request):
        self._validate_request(request)
        self._request('POST', '/pulls', dict(title=request.title, body=request.body,
                                           head=request.branch, base='main', draft=False))


class GitHubResultPublisher:
    """Publish one exact metadata-only result PR; never run or merge anything."""
    def __init__(self, transport, *, persist_state):
        if not callable(persist_state):
            raise ContractError('durable publication journal callback required')
        self.transport = transport
        self.persist_state = persist_state

    @staticmethod
    def _verify_pr(request, state, pr):
        try:
            number = _int(pr['number'])
            if (pr['state'] != 'open' or pr['draft'] is not False or pr.get('merged') is not False
                    or pr['head']['ref'] != request.branch or pr['head']['sha'] != state.head_sha
                    or pr['head']['repo']['full_name'] != REPOSITORY
                    or pr['base']['ref'] != 'main' or pr['base']['sha'] != request.base_sha
                    or pr['base']['repo']['full_name'] != REPOSITORY
                    or pr['title'] != request.title or pr['body'] != request.body
                    or pr['html_url'] != f'https://github.com/{REPOSITORY}/pull/{number}'):
                raise RemoteConflict('result PR binding mismatch')
            return number
        except (KeyError, TypeError, ContractError):
            raise RemoteConflict('result PR could not be verified') from None

    def publish(self, request, state=None, *, authorized=False, authorize_callback=None):
        """Recheck current manager approval immediately before every external write.

        The callback must return exactly True; exceptions, missing callbacks and
        other return values deny writes. Existing unknown intents may still be
        reconciled read-only after revocation, without repeating an operation.
        """
        if type(request) is not PublicationRequest:
            raise ContractError('strict publication request required')
        if state is None:
            state = PublicationState(request.digest, request.branch)
        if type(state) is not PublicationState or state.request_digest != request.digest or state.branch != request.branch:
            raise ContractError('publication journal binding changed')

        def save(**changes):
            nonlocal state
            new_state = replace(state, **changes)
            self.persist_state(new_state)
            state = new_state

        if state.phase in ('PUBLISHED', 'BLOCKED'):
            return state
        unknown = state.phase in ('BRANCH_UNKNOWN', 'CONTENT_UNKNOWN', 'PR_UNKNOWN')
        if not unknown and (authorized is not True or not callable(authorize_callback)):
            save(phase='BLOCKED', reason='AUTHORIZATION_MISSING')
            return state
        deadline = time.monotonic() + 120

        def call(method, *args):
            if time.monotonic() >= deadline:
                raise TransportUnknown('publication pass deadline exceeded')
            return getattr(self.transport, method)(*args)

        def write(method, *args):
            permitted = False
            if authorized is True and callable(authorize_callback):
                try:
                    permitted = authorize_callback() is True
                except Exception:
                    # A source error is not approval; do not echo its content.
                    permitted = False
            if not permitted:
                # No transport call was made for this intent. Any previously
                # uncertain write was read back before reaching a new write.
                save(phase='BLOCKED', reason='AUTHORIZATION_MISSING')
                return False
            call(method, *args)
            return True

        def base_current():
            if call('base_sha') != request.base_sha:
                save(phase='BLOCKED', reason='BASE_CHANGED')
                return False
            return True

        def content_verified(head):
            if head is None or head == request.base_sha:
                return False
            call('verify_commit', request, head)
            return True

        try:
            if state.phase == 'NEW':
                if not base_current():
                    return state
                head = call('branch_sha', request)
                if head is None:
                    # Persist intent before the call. Even 404 readback will not
                    # reset this to NEW after a crash/timeout.
                    save(phase='BRANCH_UNKNOWN')
                    try:
                        write('create_branch', request)
                    except TransportUnknown:
                        pass
                elif head == request.base_sha:
                    save(phase='BRANCH_READY')
                elif content_verified(head):
                    save(phase='CONTENT_READY', head_sha=head)
            if state.phase == 'BRANCH_UNKNOWN':
                head = call('branch_sha', request)
                if head is None:
                    return state
                if head == request.base_sha:
                    save(phase='BRANCH_READY')
                elif content_verified(head):
                    save(phase='CONTENT_READY', head_sha=head)
            if state.phase == 'BRANCH_READY':
                if not base_current():
                    return state
                head = call('branch_sha', request)
                if head != request.base_sha:
                    if content_verified(head):
                        save(phase='CONTENT_READY', head_sha=head)
                    else:
                        raise RemoteConflict('result branch disappeared')
                else:
                    if call('content_at', request, head) is not None:
                        raise RemoteConflict('result path already exists at base')
                    save(phase='CONTENT_UNKNOWN')
                    try:
                        write('create_content', request)
                    except TransportUnknown:
                        pass
            if state.phase == 'CONTENT_UNKNOWN':
                head = call('branch_sha', request)
                if not content_verified(head):
                    return state
                save(phase='CONTENT_READY', head_sha=head)
            if state.phase == 'CONTENT_READY':
                pr = call('find_pr', request)
                if pr is not None:
                    call('verify_commit', request, state.head_sha)
                    number = self._verify_pr(request, state, pr)
                    save(phase='PUBLISHED', pr_number=number)
                    return state
                if not base_current():
                    return state
                if call('branch_sha', request) != state.head_sha:
                    raise RemoteConflict('result head changed')
                call('verify_commit', request, state.head_sha)
                save(phase='PR_UNKNOWN')
                try:
                    write('create_pr', request)
                except TransportUnknown:
                    pass
            if state.phase == 'PR_UNKNOWN':
                pr = call('find_pr', request)
                if pr is None:
                    return state
                call('verify_commit', request, state.head_sha)
                number = self._verify_pr(request, state, pr)
                save(phase='PUBLISHED', pr_number=number)
            return state
        except TransportUnknown:
            return state
        except RemoteConflict:
            save(phase='BLOCKED', reason='REMOTE_CONFLICT')
            return state
