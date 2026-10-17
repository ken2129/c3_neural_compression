"""One fixed v0.160.0 app-server canary; never an implicit sandbox fallback.

externalSandbox does not enforce a firewall, even with networkAccess=restricted.
The caller must explicitly admit a separately verified external boundary, auth,
spending risk, resource/lifetime enforcement and whole-namespace restart. This
module neither logs in nor loads credentials nor starts arbitrary processes.
Sources: openai/codex tag rust-v0.160.0, app-server-protocol/schema/typescript/
{InitializeParams,v2/ThreadStartParams,v2/TurnStartParams,v2/Turn}.ts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import re
import selectors
import shlex
import time

from research_harness.codex_adapter import CanaryCandidate, CONFIG, WORKER_SCHEMA
from research_harness.schema import ContractError, canonical, fields, loads

BINARY = '/opt/codex/bin/codex'
WORKSPACE = '/work/canary'
CODEX_HOME = '/work/codex-home'
VERSION = '0.160.0'
MAX_FRAME = 65536
MAX_TOTAL = 1024 * 1024
COMMAND = ('python3', '-I', '-S', '-c', 'print(sum(i*i for i in range(10000)))')
SHELL_BODIES = frozenset((
    "python3 -I -S -c 'print(sum(i*i for i in range(10000)))'",
    'python3 -I -S -c "print(sum(i*i for i in range(10000)))"',
))
PROMPT = (
    'Perform only this bounded CPU canary in the explicitly admitted external '
    'sandbox. Do not inspect files, use the network, install anything, create '
    'background processes, change files, or delegate. Run exactly '
    'python3 -I -S -c "print(sum(i*i for i in range(10000)))" once. '
    'If blocked, report failure without changing permissions or retrying. '
    'Return only JSON: schema_version c3-cpu-canary-worker-v1, outcome success '
    'or failure, reported_checksum equal to the observed integer on success '
    'or null on failure. This report is only an untrusted candidate.'
)


class AppServerBlocked(ContractError):
    pass


@dataclass(frozen=True)
class AppServerRequest:
    model: str
    argv: tuple[str, ...]
    cwd: str = WORKSPACE
    codex_home: str = CODEX_HOME
    expected_version: str = VERSION
    external_network: str = 'enabled'
    wall_seconds: int = 120
    max_frame_bytes: int = MAX_FRAME
    max_capture_bytes: int = MAX_TOTAL


@dataclass(frozen=True)
class AppServerCandidate:
    candidate: CanaryCandidate
    thread_id: str
    turn_id: str
    observed_command: str | None
    command_exit_code: int | None
    command_checksum: int | None
    requires_namespace_restart: bool = field(default=True, init=False)
    cleanup_verified: bool = field(default=False, init=False)


@dataclass(frozen=True)
class PendingAppServerResult:
    candidate: CanaryCandidate
    thread_id: str
    turn_id: str
    observed_command: str | None
    command_exit_code: int | None
    command_checksum: int | None
    elapsed_seconds: float
    requires_namespace_restart: bool = field(default=True, init=False)
    cleanup_verified: bool = field(default=False, init=False)


def fixed_app_server_request(model):
    if (type(model) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', model)
            or model.endswith('-latest')):
        raise AppServerBlocked('an explicitly approved pinned model is required')
    # app-server has no exec-style --ignore-user-config. The protected launcher
    # must provide fresh/verified CODEX_HOME and clean repository/image config.
    argv = [BINARY, 'app-server', '--listen', 'stdio://', '--strict-config']
    extra = tuple(f'features.{name}=false' for name in (
        'codex_hooks', 'plugin_hooks', 'plugins', 'apps', 'connectors', 'memory_tool',
        'shell_snapshot', 'shell_snapshot_v2', 'skill_mcp_dependency_install',
        'skill_env_var_dependency_prompt', 'remote_control', 'daemon_auto_start'))
    for setting in (*CONFIG, *extra, 'features.skip_host_skill_discovery=true',
                    'sandbox_mode="read-only"', 'forced_login_method="chatgpt"',
                    'cli_auth_credentials_store="file"'):
        argv.extend(('-c', setting))
    return AppServerRequest(model, tuple(argv))


def validate_app_server_request(request):
    if type(request) is not AppServerRequest or request != fixed_app_server_request(request.model):
        raise AppServerBlocked('only the fixed app-server canary configuration is accepted')
    return request


def _finite_json(value, depth=0):
    if depth > 64 or (type(value) is float and not math.isfinite(value)):
        raise AppServerBlocked('nonfinite or excessively nested app-server JSON')
    if type(value) is dict:
        for item in value.values(): _finite_json(item, depth + 1)
    elif type(value) is list:
        for item in value: _finite_json(item, depth + 1)
    return value


class BoundedStdioTransport:
    """Binary pipes of an already-admitted worker, with incremental hard caps.

    No process creation or ownership claims. close() closes only its selector;
    the owning supervisor must stop the allocation and close its private pipes.
    Every read/write checks revocation and the same absolute monotonic deadline.
    stderr is counted and discarded, never included in an exception or log.
    """

    def __init__(self, process, *, recheck, deadline, clock=time.monotonic,
                 max_frame_bytes=MAX_FRAME, max_capture_bytes=MAX_TOTAL):
        if (not callable(recheck) or not callable(clock)
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or type(max_frame_bytes) is not int or not 1 <= max_frame_bytes <= MAX_FRAME
                or type(max_capture_bytes) is not int or not 1 <= max_capture_bytes <= MAX_TOTAL):
            raise AppServerBlocked('invalid bounded transport contract')
        self.process, self.recheck, self.deadline, self.clock = process, recheck, deadline, clock
        self.max_frame, self.max_total = max_frame_bytes, max_capture_bytes
        self.total = self.sent = self.messages = 0
        self.buffer = bytearray()
        self.eof = False
        self.selector = selectors.DefaultSelector()
        try:
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(process, name, None)
                if stream is None:
                    raise AppServerBlocked('separate binary stdio pipes are required')
                os.set_blocking(stream.fileno(), False)
                if name != 'stdin':
                    self.selector.register(stream, selectors.EVENT_READ, name)
        except BaseException:
            self.close()
            raise

    def _check(self):
        self.recheck()
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise AppServerBlocked('app-server deadline exceeded')
        return min(0.05, remaining)

    def _pump(self, outgoing=None):
        timeout = self._check()
        for key, _ in self.selector.select(timeout):
            if key.data == 'stdin':
                try:
                    written = os.write(key.fileobj.fileno(), outgoing)
                    del outgoing[:written]
                except BlockingIOError:
                    pass
                except (BrokenPipeError, OSError) as exc:
                    raise AppServerBlocked('app-server input pipe failed') from exc
                continue
            try:
                data = os.read(key.fileobj.fileno(), min(65536, self.max_total - self.total + 1))
            except BlockingIOError:
                continue
            if not data:
                self.selector.unregister(key.fileobj)
                if key.data == 'stdout': self.eof = True
                continue
            self.total += len(data)
            if self.total > self.max_total:
                raise AppServerBlocked('combined app-server output exceeds its limit')
            if key.data == 'stdout':
                self.buffer.extend(data)
                if any(len(part) > self.max_frame for part in self.buffer.split(b'\n')):
                    raise AppServerBlocked('app-server frame exceeds its limit')

    def send(self, message):
        data = bytearray(canonical(message) + b'\n')
        self.sent += len(data)
        if len(data) > self.max_frame or self.sent > MAX_FRAME * 2:
            raise AppServerBlocked('app-server request exceeds its limit')
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, 'stdin')
        try:
            while data:
                self._pump(data)
                if self.process.poll() is not None:
                    raise AppServerBlocked('app-server exited while writing')
        finally:
            self.selector.unregister(self.process.stdin)

    def receive(self):
        while b'\n' not in self.buffer:
            self._check()
            if self.eof:
                raise AppServerBlocked('app-server closed before protocol completion')
            self._pump()
        self._check()
        line, _, remaining = self.buffer.partition(b'\n')
        self.buffer = bytearray(remaining)
        self.messages += 1
        if self.messages > 2048 or not line or len(line) > self.max_frame:
            raise AppServerBlocked('invalid or excessive app-server frames')
        try:
            value = _finite_json(loads(line.decode('utf-8')))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise AppServerBlocked('invalid app-server JSON frame') from exc
        if type(value) is not dict:
            raise AppServerBlocked('app-server frame must be an object')
        return value

    def close(self):
        self.selector.close()


def _opaque(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise AppServerBlocked('invalid app-server identifier')
    return value


def _object(value):
    if type(value) is not dict:
        raise AppServerBlocked('expected an app-server object')
    return value


def _fixed_command(item):
    command = item.get('command')
    try:
        tokens = tuple(shlex.split(command)) if type(command) is str else ()
    except ValueError as exc:
        raise AppServerBlocked('malformed command event') from exc
    if (len(tokens) == 3 and tokens[0] in ('/bin/bash', '/bin/sh')
            and tokens[1] in ('-c', '-lc')):
        # v0.160 emits shlex_join(raw argv), retaining a shell-tool wrapper.
        # Token equality alone is insufficient for a shell body: an unquoted
        # newline is a command separator although shlex.split treats it as
        # whitespace. Admit only these two exact, fully quoted literal bodies.
        if tokens[2] not in SHELL_BODIES:
            raise AppServerBlocked('shell wrapper body is not the literal CPU canary')
        tokens = tuple(shlex.split(tokens[2]))
    if tokens != COMMAND or item.get('cwd') != WORKSPACE:
        raise AppServerBlocked('command event differs from the literal CPU canary')
    return command


def _candidate(text):
    if type(text) is not str or len(text.encode('utf-8')) > MAX_FRAME:
        raise AppServerBlocked('invalid final message')
    try:
        value = _finite_json(loads(text))
        fields(value, ('schema_version', 'outcome', 'reported_checksum'))
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise AppServerBlocked('invalid canary candidate JSON') from exc
    checksum = value['reported_checksum']
    if (value['schema_version'] != 'c3-cpu-canary-worker-v1'
            or value['outcome'] not in ('success', 'failure')
            or (value['outcome'] == 'success' and (type(checksum) is not int or checksum < 0))
            or (value['outcome'] == 'failure' and checksum is not None)):
        raise AppServerBlocked('invalid canary candidate')
    return CanaryCandidate(value['outcome'], checksum)


class AppServerAdapter:
    """Single thread/turn, fixed input. Transport is injected by the UID owner."""

    def __init__(self, *, enabled=False):
        self.enabled = enabled
        self.used = False
        self._admitted_request = None

    def admit(self, request, *, admit_external, recheck):
        """One protected admission before spawn; bind this immutable request.

        This in-memory binding is not deployment evidence itself. The callback
        must independently establish the external boundary, authority and risks.
        It is never re-run after the server can write fresh runtime-home state.
        """
        if self.enabled is not True:
            raise AppServerBlocked('APP_SERVER_DISABLED')
        validate_app_server_request(request)
        if not callable(admit_external) or not callable(recheck):
            raise AppServerBlocked('EXTERNAL_BOUNDARY_ADMISSION_NOT_CONFIGURED')
        if self.used or self._admitted_request is not None:
            raise AppServerBlocked('app-server adapter cannot be reused')
        recheck()
        if admit_external(request) is not None:
            raise AppServerBlocked('external boundary admission cannot be a boolean flag')
        recheck()
        self._admitted_request = request

    def run(self, request, transport, *, admit_external=None, recheck):
        if self.enabled is not True:
            raise AppServerBlocked('APP_SERVER_DISABLED')
        validate_app_server_request(request)
        if self.used:
            raise AppServerBlocked('app-server adapter cannot be reused')
        if self._admitted_request is None:
            self.admit(request, admit_external=admit_external, recheck=recheck)
        elif self._admitted_request is not request or admit_external is not None:
            raise AppServerBlocked('admitted app-server request binding mismatch')
        if not callable(recheck):
            raise AppServerBlocked('trusted approval recheck is required')
        recheck()
        self.used = True
        pending = []

        def receive():
            recheck()
            value = transport.receive()
            if type(value) is not dict:
                raise AppServerBlocked('invalid protocol object')
            if 'jsonrpc' in value and value['jsonrpc'] != '2.0':
                raise AppServerBlocked('invalid protocol version')
            if 'method' in value and 'id' in value:
                # Never grant command/file/permission/MCP/user-input/auth requests.
                transport.send({'id': value['id'], 'error': {
                    'code': -32601, 'message': 'Server requests are disabled for this canary'}})
                raise AppServerBlocked('app-server requested an unsupported capability')
            if 'error' in value or value.get('method') == 'error':
                raise AppServerBlocked('app-server reported a protocol or turn error')
            return value

        def rpc(number, method, params):
            transport.send({'id': number, 'method': method, 'params': params})
            while True:
                value = receive()
                if 'id' in value:
                    if (type(value['id']) is not int or value['id'] != number
                            or set(value) - {'jsonrpc', 'id', 'result'} or type(value.get('result')) is not dict):
                        raise AppServerBlocked('mismatched app-server response')
                    return value['result']
                if len(pending) >= 64:
                    raise AppServerBlocked('too many notifications before response')
                pending.append(value)

        initialized = rpc(1, 'initialize', {'clientInfo': {
            'name': 'c3-fixed-canary', 'title': 'C3 fixed canary', 'version': '1'},
            'capabilities': {'experimentalApi': False, 'requestAttestation': False}})
        if (initialized.get('codexHome') != CODEX_HOME or initialized.get('platformOs') != 'linux'
                or initialized.get('platformFamily') != 'unix' or not isinstance(initialized.get('userAgent'), str)):
            raise AppServerBlocked('app-server initialization configuration mismatch')
        if any(item.get('method') != 'account/updated' for item in pending):
            raise AppServerBlocked('unexpected activity before initialization')
        transport.send({'method': 'initialized'})
        result = rpc(2, 'thread/start', {'model': request.model, 'cwd': WORKSPACE,
            'approvalPolicy': 'never', 'approvalsReviewer': 'user', 'sandbox': 'read-only',
            'ephemeral': True, 'developerInstructions': PROMPT,
            'config': {'project_doc_max_bytes': 0}})
        thread = _object(result.get('thread'))
        thread_id = _opaque(thread.get('id'))
        if (thread.get('ephemeral') is not True or thread.get('cliVersion') != VERSION
                or thread.get('cwd') != WORKSPACE or thread.get('turns') != []
                or result.get('cwd') != WORKSPACE or result.get('model') != request.model
                or result.get('approvalPolicy') != 'never' or result.get('approvalsReviewer') != 'user'
                or result.get('instructionSources') != []
                or _object(result.get('sandbox')).get('type') != 'readOnly'):
            raise AppServerBlocked('thread configuration or instruction sources mismatch')
        for value in pending:
            method, params = value.get('method'), _object(value.get('params'))
            if method == 'account/updated': continue
            if method == 'thread/started':
                if _object(params.get('thread')).get('id') == thread_id: continue
            if method == 'thread/status/changed' and params.get('threadId') == thread_id: continue
            raise AppServerBlocked('unexpected activity before the fixed turn')
        result = rpc(3, 'turn/start', {'threadId': thread_id, 'model': request.model,
            'cwd': WORKSPACE, 'approvalPolicy': 'never', 'approvalsReviewer': 'user',
            'sandboxPolicy': {'type': 'externalSandbox', 'networkAccess': request.external_network},
            'input': [{'type': 'text', 'text': PROMPT, 'text_elements': []}],
            'outputSchema': WORKER_SCHEMA})
        turn = _object(result.get('turn'))
        turn_id = _opaque(turn.get('id'))
        if turn.get('status') != 'inProgress' or turn.get('error') is not None:
            raise AppServerBlocked('turn did not start in progress')
        started = False
        final = None
        command_event = None
        command_id = None
        started_items, completed_items = {}, {}
        allowed_items = {'userMessage', 'agentMessage', 'reasoning', 'commandExecution'}
        while True:
            value = pending.pop(0) if pending else receive()
            if set(value) - {'jsonrpc', 'method', 'params'} or type(value.get('params')) is not dict:
                raise AppServerBlocked('invalid app-server notification')
            method, params = value.get('method'), value['params']
            if method == 'account/updated':
                continue  # Notification is not auth evidence; no auth RPC is sent.
            if method == 'thread/started':
                notified = _object(params.get('thread'))
                if notified.get('id') != thread_id or notified.get('ephemeral') is not True:
                    raise AppServerBlocked('unexpected thread notification')
                continue
            if params.get('threadId') != thread_id:
                raise AppServerBlocked('notification thread mismatch')
            if method in ('thread/status/changed', 'thread/tokenUsage/updated'):
                if 'turnId' in params and params['turnId'] != turn_id:
                    raise AppServerBlocked('notification turn mismatch')
                continue
            if method in ('turn/started', 'turn/completed'):
                event_turn = _object(params.get('turn'))
                if event_turn.get('id') != turn_id or event_turn.get('error') is not None:
                    raise AppServerBlocked('unexpected or failed turn notification')
                if method == 'turn/started':
                    if started or event_turn.get('status') != 'inProgress':
                        raise AppServerBlocked('duplicate or invalid turn start')
                    started = True
                    continue
                if not started or event_turn.get('status') != 'completed' or final is None:
                    raise AppServerBlocked('turn completed without a valid final candidate')
                if started_items.keys() != completed_items.keys():
                    raise AppServerBlocked('turn completed with unfinished items')
                # Streamed item completion is authoritative; any final items
                # supplied in the completion snapshot must agree with it.
                items = event_turn.get('items')
                if type(items) is not list:
                    raise AppServerBlocked('invalid completed-turn items')
                snapshot_ids = set()
                for item in items:
                    item = _object(item)
                    snapshot_id = _opaque(item.get('id'))
                    if snapshot_id in snapshot_ids or completed_items.get(snapshot_id) != item:
                        raise AppServerBlocked('conflicting or unobserved final item snapshot')
                    snapshot_ids.add(snapshot_id)
                recheck()
                candidate = _candidate(final[1])
                if candidate.outcome == 'success' and (command_event is None or command_event[1] != 0):
                    raise AppServerBlocked('success candidate lacks matching command completion')
                if candidate.outcome == 'success' and (
                        command_event[2] != sum(i*i for i in range(10000))
                        or candidate.reported_checksum != command_event[2]):
                    raise AppServerBlocked('command event and candidate checksum do not agree')
                return AppServerCandidate(candidate, thread_id, turn_id,
                    command_event[0] if command_event else None,
                    command_event[1] if command_event else None,
                    command_event[2] if command_event else None)
            if params.get('turnId') != turn_id or not started:
                raise AppServerBlocked('item turn mismatch or event before turn start')
            if method in ('item/started', 'item/completed'):
                item = _object(params.get('item'))
                if item.get('type') not in allowed_items:
                    raise AppServerBlocked('unexpected canary tool or item')
                item_id = _opaque(item.get('id'))
                if item['type'] == 'commandExecution':
                    command = _fixed_command(item)
                if method == 'item/started':
                    if item_id in started_items:
                        raise AppServerBlocked('duplicate item start')
                    started_items[item_id] = item
                    if item['type'] == 'commandExecution':
                        if command_id is not None:
                            raise AppServerBlocked('more than one command was attempted')
                        command_id = item_id
                if method == 'item/completed':
                    prior = started_items.get(item_id)
                    if prior is None or prior.get('type') != item['type'] or item_id in completed_items:
                        raise AppServerBlocked('missing item start or duplicate item completion')
                    completed_items[item_id] = item
                    if item['type'] == 'commandExecution':
                        if command_event is not None or prior.get('command') != command:
                            raise AppServerBlocked('more than one command was attempted')
                        code = item.get('exitCode')
                        if item.get('status') != 'completed' or type(code) is not int:
                            raise AppServerBlocked('canary command did not complete')
                        output = item.get('aggregatedOutput')
                        checksum = (int(output.strip()) if type(output) is str and len(output) <= 32
                                    and re.fullmatch(r'[0-9]+\n?', output) else None)
                        command_event = (command, code, checksum)
                    if item['type'] == 'agentMessage' and item.get('phase') == 'final_answer':
                        if final is not None or type(item.get('text')) is not str:
                            raise AppServerBlocked('duplicate or malformed final answer')
                        final = (item_id, item['text'])
                continue
            if method in ('item/agentMessage/delta', 'item/reasoning/summaryTextDelta',
                          'item/reasoning/textDelta', 'item/reasoning/summaryPartAdded',
                          'item/commandExecution/outputDelta', 'item/commandExecution/terminalInteraction'):
                item_id = _opaque(params.get('itemId'))
                prior = started_items.get(item_id)
                expected = method.split('/')[1]
                if prior is None or prior.get('type') != expected or item_id in completed_items:
                    raise AppServerBlocked('delta item identity or lifecycle mismatch')
                continue  # Bound/count bytes, never assemble deltas as evidence.
            raise AppServerBlocked('unsupported app-server notification')
