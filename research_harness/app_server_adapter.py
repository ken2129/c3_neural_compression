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
from contextvars import ContextVar
from functools import wraps
from types import MappingProxyType
from weakref import WeakKeyDictionary
import math
import os
import re
import selectors
import shlex
import time

from research_harness.codex_adapter import CanaryCandidate, CONFIG, WORKER_SCHEMA
from research_harness.schema import ContractError, canonical, fields, loads
from research_harness.manager_runtime import (RECHECK_REASONS, RECHECK_ORIGINS,
                                               safe_recheck_failure)

BINARY = '/opt/codex/bin/codex'
WORKSPACE = '/work/canary'
CODEX_HOME = '/work/codex-home'
VERSION = '0.160.0'
MAX_FRAME = 65536
MAX_TOTAL = 1024 * 1024
# Exact rust-v0.160.0 startup fallback notice, observed in the no-model probe.
# This admits metadata only; it neither proves a sandbox works nor changes it.
_BUNDLED_BWRAP_WARNING = (
    'Codex could not find bubblewrap on PATH. Install bubblewrap with your OS '
    'package manager. See the sandbox prerequisites: '
    'https://developers.openai.com/codex/concepts/sandboxing#prerequisites. '
    'Codex will use the bundled bubblewrap in the meantime.'
)
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


# Fixed reason vocabulary: values are diagnostic identifiers, not exception text.
# Keep existing codes stable; the AST coverage test rejects unmapped raise sites.
_BLOCK_REASON_CODES = MappingProxyType({
    'an explicitly approved pinned model is required': 'APP_SERVER_MODEL_NOT_PINNED',
    'only the fixed app-server canary configuration is accepted': 'APP_SERVER_FIXED_REQUEST_REQUIRED',
    'nonfinite or excessively nested app-server JSON': 'APP_SERVER_JSON_DEPTH_OR_NONFINITE',
    'invalid bounded transport contract': 'APP_SERVER_TRANSPORT_CONTRACT',
    'separate binary stdio pipes are required': 'APP_SERVER_STDIO_PIPES_REQUIRED',
    'app-server deadline exceeded': 'APP_SERVER_DEADLINE_EXCEEDED',
    'app-server input pipe failed': 'APP_SERVER_INPUT_PIPE_FAILED',
    'combined app-server output exceeds its limit': 'APP_SERVER_OUTPUT_LIMIT',
    'app-server frame exceeds its limit': 'APP_SERVER_FRAME_LIMIT',
    'app-server request exceeds its limit': 'APP_SERVER_REQUEST_LIMIT',
    'app-server exited while writing': 'APP_SERVER_EXITED_WRITING',
    'app-server closed before protocol completion': 'APP_SERVER_EARLY_EOF',
    'invalid or excessive app-server frames': 'APP_SERVER_FRAME_COUNT_OR_FORMAT',
    'invalid app-server JSON frame': 'APP_SERVER_JSON_FRAME_INVALID',
    'app-server frame must be an object': 'APP_SERVER_FRAME_NOT_OBJECT',
    'invalid app-server identifier': 'APP_SERVER_IDENTIFIER_INVALID',
    'expected an app-server object': 'APP_SERVER_OBJECT_REQUIRED',
    'invalid app-server notification': 'APP_SERVER_NOTIFICATION_INVALID',
    'invalid app-server notification timestamp': 'APP_SERVER_NOTIFICATION_TIMESTAMP',
    'invalid app-server telemetry fields': 'APP_SERVER_TELEMETRY_FIELDS',
    'app-server configuration warning rejected': 'APP_SERVER_CONFIG_WARNING_REJECTED',
    'invalid account telemetry': 'APP_SERVER_ACCOUNT_TELEMETRY',
    'remote control is not disabled or telemetry is malformed': 'APP_SERVER_REMOTE_CONTROL_STATE',
    'invalid rate-limit telemetry': 'APP_SERVER_RATE_LIMIT_TELEMETRY',
    'invalid rate-limit window telemetry': 'APP_SERVER_RATE_WINDOW_TELEMETRY',
    'invalid credits telemetry': 'APP_SERVER_CREDITS_TELEMETRY',
    'invalid spend-limit telemetry': 'APP_SERVER_SPEND_LIMIT_TELEMETRY',
    'invalid or failed thread status': 'APP_SERVER_THREAD_STATUS',
    'invalid thread status flags': 'APP_SERVER_THREAD_STATUS_FLAGS',
    'invalid token-usage telemetry': 'APP_SERVER_TOKEN_USAGE',
    'invalid model-verification telemetry': 'APP_SERVER_MODEL_VERIFICATION',
    'invalid safety-buffering telemetry': 'APP_SERVER_SAFETY_BUFFERING',
    'invalid item-delta telemetry': 'APP_SERVER_ITEM_DELTA',
    'malformed command event': 'APP_SERVER_COMMAND_MALFORMED',
    'shell wrapper body is not the literal CPU canary': 'APP_SERVER_SHELL_BODY',
    'command event differs from the literal CPU canary': 'APP_SERVER_COMMAND_MISMATCH',
    'invalid final message': 'APP_SERVER_FINAL_MESSAGE',
    'invalid canary candidate JSON': 'APP_SERVER_CANDIDATE_JSON',
    'invalid canary candidate': 'APP_SERVER_CANDIDATE_SCHEMA',
    'APP_SERVER_DISABLED': 'APP_SERVER_DISABLED',
    'EXTERNAL_BOUNDARY_ADMISSION_NOT_CONFIGURED': 'APP_SERVER_EXTERNAL_ADMISSION_MISSING',
    'app-server adapter cannot be reused': 'APP_SERVER_ADAPTER_REUSED',
    'external boundary admission cannot be a boolean flag': 'APP_SERVER_EXTERNAL_ADMISSION_BOOLEAN',
    'admitted app-server request binding mismatch': 'APP_SERVER_REQUEST_BINDING',
    'trusted approval recheck is required': 'APP_SERVER_RECHECK_REQUIRED',
    'trusted approval recheck failed': 'APP_SERVER_RECHECK_FAILED',
    'app-server boundary contract failed': 'APP_SERVER_BOUNDARY_CONTRACT',
    'invalid protocol object': 'APP_SERVER_PROTOCOL_OBJECT',
    'invalid protocol version': 'APP_SERVER_PROTOCOL_VERSION',
    'app-server requested an unsupported capability': 'APP_SERVER_SERVER_REQUEST',
    'app-server reported a protocol or turn error': 'APP_SERVER_SERVER_ERROR',
    'mismatched app-server response': 'APP_SERVER_RESPONSE_MISMATCH',
    'too many notifications before response': 'APP_SERVER_PRE_RESPONSE_LIMIT',
    'app-server initialization configuration mismatch': 'APP_SERVER_INITIALIZE_CONFIGURATION',
    'unexpected activity before initialization': 'APP_SERVER_PRE_INITIALIZE_ACTIVITY',
    'thread configuration or instruction sources mismatch': 'APP_SERVER_THREAD_CONFIGURATION',
    'unexpected activity before the fixed turn': 'APP_SERVER_PRE_TURN_ACTIVITY',
    'turn did not start in progress': 'APP_SERVER_TURN_START_STATUS',
    'unexpected thread notification': 'APP_SERVER_THREAD_NOTIFICATION',
    'notification thread mismatch': 'APP_SERVER_THREAD_MISMATCH',
    'notification turn mismatch': 'APP_SERVER_TURN_MISMATCH',
    'unexpected or failed turn notification': 'APP_SERVER_TURN_NOTIFICATION',
    'duplicate or invalid turn start': 'APP_SERVER_TURN_START_DUPLICATE',
    'turn completed without a valid final candidate': 'APP_SERVER_FINAL_CANDIDATE_MISSING',
    'turn completed with unfinished items': 'APP_SERVER_UNFINISHED_ITEMS',
    'invalid completed-turn items': 'APP_SERVER_FINAL_ITEMS',
    'conflicting or unobserved final item snapshot': 'APP_SERVER_SNAPSHOT_CONFLICT',
    'incomplete final item snapshot': 'APP_SERVER_SNAPSHOT_INCOMPLETE',
    'summary does not match the streamed final candidate': 'APP_SERVER_SUMMARY_MISMATCH',
    'success candidate lacks matching command completion': 'APP_SERVER_COMMAND_COMPLETION_MISSING',
    'command event and candidate checksum do not agree': 'APP_SERVER_CHECKSUM_MISMATCH',
    'item turn mismatch or event before turn start': 'APP_SERVER_ITEM_TURN_OR_ORDER',
    'unexpected canary tool or item': 'APP_SERVER_ITEM_TYPE',
    'malformed agent message phase or text': 'APP_SERVER_AGENT_MESSAGE',
    'duplicate item start': 'APP_SERVER_ITEM_START_DUPLICATE',
    'more than one command was attempted': 'APP_SERVER_MULTIPLE_COMMANDS',
    'missing item start or duplicate item completion': 'APP_SERVER_ITEM_COMPLETION_ORDER',
    'canary command did not complete': 'APP_SERVER_COMMAND_NOT_COMPLETED',
    'ambiguous multiple final candidates': 'APP_SERVER_MULTIPLE_CANDIDATES',
    'delta item identity or lifecycle mismatch': 'APP_SERVER_DELTA_LIFECYCLE',
    'unsupported app-server notification': 'APP_SERVER_NOTIFICATION_UNSUPPORTED',
})
_OTHER_REASON = 'APP_SERVER_CONTRACT_OTHER'
_REASON_CODES = frozenset((*_BLOCK_REASON_CODES.values(), _OTHER_REASON))
_PHASES = frozenset(('unscoped', 'admission', 'initialize_send', 'initialize_wait',
    'initialize_validate', 'thread_start_send', 'thread_start_wait',
    'thread_start_validate', 'turn_start_send', 'turn_start_wait',
    'turn_start_validate', 'stream', 'final_validate'))
_FRAME_KINDS = frozenset(('none', 'unknown', 'request', 'response', 'notification'))
# A classification vocabulary only: membership never admits a request or event.
_METHODS = frozenset(('none', 'unknown', 'initialize', 'initialized', 'thread/start',
    'turn/start', 'error', 'account/updated', 'account/rateLimits/updated',
    'remoteControl/status/changed', 'thread/started', 'thread/status/changed',
    'thread/tokenUsage/updated', 'turn/started', 'turn/completed',
    'model/verification', 'model/safetyBuffering/updated', 'item/started',
    'item/completed', 'item/agentMessage/delta', 'item/reasoning/summaryTextDelta',
    'item/reasoning/textDelta', 'item/reasoning/summaryPartAdded',
    'item/commandExecution/outputDelta', 'item/commandExecution/terminalInteraction',
    'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
    'item/tool/requestUserInput', 'mcpServer/elicitation/request',
    'item/permissions/requestApproval', 'item/tool/call',
    'account/chatgptAuthTokens/refresh', 'attestation/generate',
    'applyPatchApproval', 'execCommandApproval', 'warning',
    'deprecationNotice', 'configWarning'))
_CONFIG_WARNING_CATEGORIES = frozenset(('none', 'bundled_bwrap_fallback', 'unrecognized', 'malformed'))
_DIAGNOSTIC_FIELDS = ('reason', 'phase', 'frame_kind', 'method', 'initialized',
                      'thread_started', 'turn_accepted', 'final_validated', 'config_warning',
                      'recheck_reason', 'recheck_origin')
_DIAGNOSTIC_CONTEXT = ContextVar('app_server_diagnostic', default=None)
# Weak keys avoid retaining exception tracebacks. Records live outside mutable
# exception attributes; neither forged attributes nor args affect the exporter.
_DIAGNOSTIC_RECORDS = WeakKeyDictionary()


class _DiagnosticState:
    def __init__(self, phase):
        self.phase, self.frame_kind, self.method = phase, 'none', 'none'
        self.initialized = self.thread_started = self.turn_accepted = self.final_validated = False
        self.rpc_method = 'none'
        self.config_warning = 'none'
        self.recheck_reason = self.recheck_origin = 'none'

    def record(self, reason):
        return (reason, self.phase, self.frame_kind, self.method, self.initialized,
                self.thread_started, self.turn_accepted, self.final_validated, self.config_warning,
                self.recheck_reason, self.recheck_origin)


def _diagnosed(phase):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            if _DIAGNOSTIC_CONTEXT.get() is not None:
                return function(*args, **kwargs)
            token = _DIAGNOSTIC_CONTEXT.set(_DiagnosticState(phase))
            try:
                return function(*args, **kwargs)
            except AppServerBlocked as exc:
                # Also capture the current phase when a trusted callback raises
                # an exception that was constructed before this run began.
                if type(exc) is AppServerBlocked:
                    record = _DIAGNOSTIC_RECORDS.get(exc)
                    if (type(record) is tuple and len(record) == len(_DIAGNOSTIC_FIELDS)
                            and type(record[0]) is str and record[0] in _REASON_CODES):
                        _DIAGNOSTIC_RECORDS[exc] = _DIAGNOSTIC_CONTEXT.get().record(record[0])
                        raise
                # Do not trust subclass attributes or constructor-bypass data.
                # The trusted call boundary can still record a fixed failure.
                raise AppServerBlocked('app-server boundary contract failed') from None
            except ContractError:
                # Schema/canonical helpers and trusted integrations may raise
                # the base class. Preserve phase without exposing their text.
                raise AppServerBlocked('app-server boundary contract failed') from None
            finally:
                _DIAGNOSTIC_CONTEXT.reset(token)
        return call
    return decorate


def _diagnostic_phase(phase):
    state = _DIAGNOSTIC_CONTEXT.get()
    if state is not None:
        state.phase = phase


def _diagnostic_milestone(name):
    state = _DIAGNOSTIC_CONTEXT.get()
    if state is not None:
        setattr(state, name, True)


def _diagnostic_clear_frame():
    state = _DIAGNOSTIC_CONTEXT.get()
    if state is not None:
        state.frame_kind, state.method = 'none', 'none'
        state.config_warning = 'none'
        state.recheck_reason = state.recheck_origin = 'none'


def _diagnostic_frame(value):
    state = _DIAGNOSTIC_CONTEXT.get()
    if state is None:
        return
    state.frame_kind, state.method = 'unknown', 'unknown'
    state.config_warning = 'none'
    state.recheck_reason = state.recheck_origin = 'none'
    if type(value) is not dict:
        return
    if 'method' in value:
        state.frame_kind = 'request' if 'id' in value else 'notification'
        method = value['method']
        if type(method) is str and len(method) <= 64 and method in _METHODS:
            state.method = method
    elif 'id' in value:
        state.frame_kind, state.method = 'response', state.rpc_method


class AppServerBlocked(ContractError):
    def __init__(self, *args):
        super().__init__(*args)
        if type(self) is AppServerBlocked:
            message = args[0] if len(args) == 1 else None
            reason = (_BLOCK_REASON_CODES.get(message, _OTHER_REASON)
                      if type(message) is str and len(message) <= 160 else _OTHER_REASON)
            state = _DIAGNOSTIC_CONTEXT.get()
            _DIAGNOSTIC_RECORDS[self] = (state or _DiagnosticState('unscoped')).record(reason)


def safe_app_server_diagnostic(exc):
    """Fresh, bounded fixed-vocabulary data, or None; never stringify input.

    This is diagnostic context, not proof that a model ran or did not run.
    In particular, a server may accept a sent turn before a response is received.
    Subclasses and exceptions bypassing our constructor have no trusted record.
    Caller mutation, exception args/attributes, causes and arbitrary event values
    cannot enter this export. Frame fields describe the current validation. Frame fields
    and config-warning category are cleared before every new frame and transport
    operation; response methods name the pending
    RPC, not a method claimed by the server. Do not log exception text alongside
    this record.
    """
    if type(exc) is not AppServerBlocked:
        return None
    record = _DIAGNOSTIC_RECORDS.get(exc)
    if type(record) is not tuple or len(record) != len(_DIAGNOSTIC_FIELDS):
        return None
    for value, allowed in zip(record[:4], (_REASON_CODES, _PHASES, _FRAME_KINDS, _METHODS)):
        if type(value) is not str or len(value) > 64 or value not in allowed:
            return None
    category = record[8]
    if type(category) is not str or len(category) > 32 or category not in _CONFIG_WARNING_CATEGORIES:
        return None
    for value, allowed in zip(record[9:], (RECHECK_REASONS, RECHECK_ORIGINS)):
        if type(value) is not str or len(value) > 32 or (value != 'none' and value not in allowed):
            return None
    if (record[9] == 'none') != (record[10] == 'none'):
        return None
    flags = record[4:8]
    if any(type(flag) is not bool for flag in flags):
        return None
    if any(flags[index] and not flags[index - 1] for index in range(1, len(flags))):
        return None
    return dict(zip(_DIAGNOSTIC_FIELDS, record))


@_diagnosed('unscoped')
def _checked_recheck(callback):
    """Attach fixed manager/source/IPC context to a failed trusted check.

    This does not cache, retry or reinterpret approval. Process-control signals
    still propagate as BaseException; ordinary callback failures stay blocked.
    """
    try:
        callback()
    except Exception as exc:
        failure = safe_recheck_failure(exc, origin='approval_callback')
        state = _DIAGNOSTIC_CONTEXT.get()
        state.recheck_reason = failure['reason']
        state.recheck_origin = failure['origin']
        raise AppServerBlocked('trusted approval recheck failed') from None


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
        'plugin_hooks', 'plugins', 'apps',
        'shell_snapshot', 'shell_snapshot_v2', 'skill_mcp_dependency_install',
        'skill_env_var_dependency_prompt', 'remote_control', 'daemon_auto_start'))
    for setting in (*CONFIG, *extra, 'features.skip_host_skill_discovery=true',
                    'suppress_unstable_features_warning=true',
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
        _checked_recheck(self.recheck)
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


def _notification_envelope(value):
    # rust-v0.160.0 ServerNotificationEnvelope: current servers include this
    # timestamp; omission remains supported for older serialized notifications.
    if (set(value) - {'jsonrpc', 'method', 'params', 'emittedAtMs'}
            or type(value.get('method')) is not str or type(value.get('params')) is not dict):
        raise AppServerBlocked('invalid app-server notification')
    if 'emittedAtMs' in value:
        timestamp = value['emittedAtMs']
        if type(timestamp) is not int or not 0 <= timestamp <= 2**63 - 1:
            raise AppServerBlocked('invalid app-server notification timestamp')


def _metadata_object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise AppServerBlocked('invalid app-server telemetry fields')
    return value


def _telemetry_percentage(value):
    # v0.160 account.rs serializes these rounded percentages as Rust i32.
    return type(value) is int and -(2**31) <= value < 2**31


def _config_warning(params):
    """Only the pinned bundled-bwrap notice is harmless connection metadata.

    ConfigWarningNotification.ts requires summary/details and permits path/range.
    The observed startup notice has no source location; absent/null locations are
    the only admitted forms. Non-null locations and all other warnings stop.
    No warning text, path or details is copied to diagnostics.
    """
    category = 'malformed'
    if (type(params) is dict and {'summary', 'details'} <= params.keys()
            and not params.keys() - {'summary', 'details', 'path', 'range'}
            and type(params['summary']) is str
            and (params['details'] is None or type(params['details']) is str)
            and (params.get('path') is None or type(params['path']) is str)
            and (params.get('range') is None or type(params['range']) is dict)):
        category = ('bundled_bwrap_fallback'
                    if params['summary'] == _BUNDLED_BWRAP_WARNING
                    and params['details'] is None
                    and params.get('path') is None and params.get('range') is None
                    else 'unrecognized')
    state = _DIAGNOSTIC_CONTEXT.get()
    if state is not None:
        state.config_warning = category
    if category != 'bundled_bwrap_fallback':
        raise AppServerBlocked('app-server configuration warning rejected')


def _connection_telemetry(method, params):
    """Validate known connection-level metadata; never treat it as evidence."""
    if method == 'configWarning':
        _config_warning(params)
        return True
    plan_types = (None, 'free', 'go', 'plus', 'pro', 'prolite', 'promax', 'team',
        'self_serve_business_prolite', 'self_serve_business_usage_based', 'business',
        'ent26', 'enterprise_cbp_automation', 'enterprise_cbp_usage_based',
        'enterprise', 'edu', 'edu_plus', 'edu_pro', 'unknown')
    if method == 'account/updated':
        _metadata_object(params, ('authMode', 'planType'))
        if (params['authMode'] not in (None, 'apikey', 'chatgpt', 'chatgptAuthTokens',
                'headers', 'agentIdentity', 'personalAccessToken', 'bedrockApiKey', 'bedrockAccessKeys')
                or params['planType'] not in plan_types):
            raise AppServerBlocked('invalid account telemetry')
        return True
    if method == 'remoteControl/status/changed':
        _metadata_object(params, ('status', 'serverName', 'installationId', 'environmentId'))
        if (params['status'] != 'disabled'
                or any(type(params[key]) is not str for key in ('serverName', 'installationId'))
                or params['environmentId'] is not None):
            raise AppServerBlocked('remote control is not disabled or telemetry is malformed')
        return True
    if method != 'account/rateLimits/updated':
        return False
    _metadata_object(params, ('rateLimits',))
    limits = _metadata_object(params['rateLimits'], ('limitId', 'limitName',
        'normalModelSlug', 'primary', 'secondary', 'credits', 'individualLimit',
        'spendControlReached', 'planType', 'rateLimitReachedType'))
    if (any(limits[key] is not None and type(limits[key]) is not str
            for key in ('limitId', 'limitName', 'normalModelSlug'))
            or limits['planType'] not in plan_types
            or limits['rateLimitReachedType'] not in (None, 'rate_limit_reached',
                'workspace_owner_credits_depleted', 'workspace_member_credits_depleted',
                'workspace_owner_usage_limit_reached', 'workspace_member_usage_limit_reached')
            or (limits['spendControlReached'] is not None
                and type(limits['spendControlReached']) is not bool)):
        raise AppServerBlocked('invalid rate-limit telemetry')
    for key in ('primary', 'secondary'):
        if limits[key] is None: continue
        window = _metadata_object(limits[key], ('usedPercent', 'windowDurationMins', 'resetsAt'))
        if (not _telemetry_percentage(window['usedPercent'])
                or any(window[name] is not None and type(window[name]) is not int
                       for name in ('windowDurationMins', 'resetsAt'))):
            raise AppServerBlocked('invalid rate-limit window telemetry')
    if limits['credits'] is not None:
        credits = _metadata_object(limits['credits'], ('hasCredits', 'unlimited', 'balance'))
        if (type(credits['hasCredits']) is not bool or type(credits['unlimited']) is not bool
                or (credits['balance'] is not None and type(credits['balance']) is not str)):
            raise AppServerBlocked('invalid credits telemetry')
    if limits['individualLimit'] is not None:
        spend = _metadata_object(limits['individualLimit'], ('limit', 'used', 'remainingPercent', 'resetsAt'))
        if (type(spend['limit']) is not str or type(spend['used']) is not str
                or not _telemetry_percentage(spend['remainingPercent']) or type(spend['resetsAt']) is not int):
            raise AppServerBlocked('invalid spend-limit telemetry')
    return True


def _thread_status(params):
    _metadata_object(params, ('threadId', 'status'))
    status = _object(params['status'])
    kind = status.get('type')
    _metadata_object(status, ('type', 'activeFlags') if kind == 'active' else ('type',))
    if kind not in ('notLoaded', 'idle', 'active'):
        raise AppServerBlocked('invalid or failed thread status')
    if kind == 'active' and (type(status['activeFlags']) is not list or any(
            flag not in ('waitingOnApproval', 'waitingOnUserInput') for flag in status['activeFlags'])):
        raise AppServerBlocked('invalid thread status flags')


def _turn_telemetry(method, params):
    if method == 'thread/tokenUsage/updated':
        _metadata_object(params, ('threadId', 'turnId', 'tokenUsage'))
        usage = _metadata_object(params['tokenUsage'], ('total', 'last', 'modelContextWindow'))
        if usage['modelContextWindow'] is not None and type(usage['modelContextWindow']) is not int:
            raise AppServerBlocked('invalid token-usage telemetry')
        for key in ('total', 'last'):
            counts = _metadata_object(usage[key], ('totalTokens', 'inputTokens', 'cachedInputTokens',
                'cacheWriteInputTokens', 'outputTokens', 'reasoningOutputTokens'))
            if any(type(count) is not int for count in counts.values()):
                raise AppServerBlocked('invalid token-usage telemetry')
        return True
    if method == 'model/verification':
        _metadata_object(params, ('threadId', 'turnId', 'verifications'))
        if type(params['verifications']) is not list or any(
                item != 'trustedAccessForCyber' for item in params['verifications']):
            raise AppServerBlocked('invalid model-verification telemetry')
        return True  # Backend metadata grants this adapter no capability.
    if method == 'model/safetyBuffering/updated':
        _metadata_object(params, ('threadId', 'turnId', 'model', 'useCases', 'reasons',
                                  'showBufferingUi', 'fasterModel'))
        if (type(params['model']) is not str or type(params['showBufferingUi']) is not bool
                or (params['fasterModel'] is not None and type(params['fasterModel']) is not str)
                or any(type(params[key]) is not list or any(type(item) is not str for item in params[key])
                       for key in ('useCases', 'reasons'))):
            raise AppServerBlocked('invalid safety-buffering telemetry')
        return True  # A suggested faster model never changes the pinned request.
    return False


def _item_delta(method, params):
    suffix = method.rsplit('/', 1)[-1]
    keys = ['threadId', 'turnId', 'itemId']
    if suffix == 'terminalInteraction':
        keys += ['processId', 'stdin']
    else:
        if suffix != 'summaryPartAdded': keys.append('delta')
        if suffix in ('summaryTextDelta', 'summaryPartAdded'): keys.append('summaryIndex')
        if suffix == 'textDelta': keys.append('contentIndex')
    _metadata_object(params, keys)
    for key in keys[3:]:
        if key in ('summaryIndex', 'contentIndex'):
            valid = type(params[key]) is int and params[key] >= 0
        else:
            valid = type(params[key]) is str
        if not valid:
            raise AppServerBlocked('invalid item-delta telemetry')


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

    @_diagnosed('admission')
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
        _checked_recheck(recheck)
        if admit_external(request) is not None:
            raise AppServerBlocked('external boundary admission cannot be a boolean flag')
        _checked_recheck(recheck)
        self._admitted_request = request

    @_diagnosed('admission')
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
        _checked_recheck(recheck)
        self.used = True
        pending = []

        def receive():
            _diagnostic_clear_frame()
            _checked_recheck(recheck)
            value = transport.receive()
            _diagnostic_frame(value)
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
            if 'id' not in value:
                _notification_envelope(value)
            return value

        def rpc(number, method, params):
            phase = {1: 'initialize', 2: 'thread_start', 3: 'turn_start'}[number]
            _DIAGNOSTIC_CONTEXT.get().rpc_method = method
            _diagnostic_phase(phase + '_send')
            _diagnostic_clear_frame()
            transport.send({'id': number, 'method': method, 'params': params})
            _diagnostic_phase(phase + '_wait')
            while True:
                value = receive()
                if 'id' in value:
                    _diagnostic_phase(phase + '_validate')
                    if (type(value['id']) is not int or value['id'] != number
                            or set(value) - {'jsonrpc', 'id', 'result'} or type(value.get('result')) is not dict):
                        raise AppServerBlocked('mismatched app-server response')
                    return value['result']
                if _connection_telemetry(value['method'], value['params']):
                    continue
                if len(pending) >= 64:
                    raise AppServerBlocked('too many notifications before response')
                pending.append(value)

        initialized = rpc(1, 'initialize', {'clientInfo': {
            'name': 'c3-fixed-canary', 'title': 'C3 fixed canary', 'version': '1'},
            'capabilities': {'experimentalApi': False, 'requestAttestation': False}})
        if (initialized.get('codexHome') != CODEX_HOME or initialized.get('platformOs') != 'linux'
                or initialized.get('platformFamily') != 'unix' or not isinstance(initialized.get('userAgent'), str)):
            raise AppServerBlocked('app-server initialization configuration mismatch')
        if pending:
            _diagnostic_frame(pending[0])
            raise AppServerBlocked('unexpected activity before initialization')
        _diagnostic_milestone('initialized')
        _diagnostic_phase('initialize_send')
        _diagnostic_clear_frame()
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
            _diagnostic_frame(value)
            method, params = value.get('method'), _object(value.get('params'))
            if method == 'thread/started':
                if _object(params.get('thread')).get('id') == thread_id: continue
            if method == 'thread/status/changed' and params.get('threadId') == thread_id:
                _thread_status(params)
                continue
            raise AppServerBlocked('unexpected activity before the fixed turn')
        _diagnostic_milestone('thread_started')
        result = rpc(3, 'turn/start', {'threadId': thread_id, 'model': request.model,
            'cwd': WORKSPACE, 'approvalPolicy': 'never', 'approvalsReviewer': 'user',
            'sandboxPolicy': {'type': 'externalSandbox', 'networkAccess': request.external_network},
            'input': [{'type': 'text', 'text': PROMPT, 'text_elements': []}],
            'outputSchema': WORKER_SCHEMA})
        turn = _object(result.get('turn'))
        turn_id = _opaque(turn.get('id'))
        if turn.get('status') != 'inProgress' or turn.get('error') is not None:
            raise AppServerBlocked('turn did not start in progress')
        _diagnostic_milestone('turn_accepted')
        _diagnostic_phase('stream')
        started = False
        final = None
        last_summary_item_id = None
        command_event = None
        command_id = None
        started_items, completed_items = {}, {}
        allowed_items = {'userMessage', 'agentMessage', 'reasoning', 'commandExecution'}
        while True:
            value = pending.pop(0) if pending else receive()
            _diagnostic_frame(value)
            _notification_envelope(value)
            method, params = value.get('method'), value['params']
            if _connection_telemetry(method, params):
                continue
            if method == 'thread/started':
                notified = _object(params.get('thread'))
                if notified.get('id') != thread_id or notified.get('ephemeral') is not True:
                    raise AppServerBlocked('unexpected thread notification')
                continue
            if params.get('threadId') != thread_id:
                raise AppServerBlocked('notification thread mismatch')
            if method == 'thread/status/changed':
                _thread_status(params)
                continue
            if _turn_telemetry(method, params):
                if params['turnId'] != turn_id:
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
                _diagnostic_phase('final_validate')
                if not started or event_turn.get('status') != 'completed' or final is None:
                    raise AppServerBlocked('turn completed without a valid final candidate')
                if started_items.keys() != completed_items.keys():
                    raise AppServerBlocked('turn completed with unfinished items')
                # v0.160 emits a summary containing only the last nonempty
                # final/unknown-phase agent message. A full view has all items.
                items = event_turn.get('items')
                view = event_turn.get('itemsView')
                if type(items) is not list or view not in ('summary', 'full'):
                    raise AppServerBlocked('invalid completed-turn items')
                snapshot_ids = set()
                for item in items:
                    item = _object(item)
                    snapshot_id = _opaque(item.get('id'))
                    if snapshot_id in snapshot_ids or completed_items.get(snapshot_id) != item:
                        raise AppServerBlocked('conflicting or unobserved final item snapshot')
                    snapshot_ids.add(snapshot_id)
                if view == 'full' and snapshot_ids != completed_items.keys():
                    raise AppServerBlocked('incomplete final item snapshot')
                if view == 'summary' and (
                        snapshot_ids != {final[0]} or last_summary_item_id != final[0]):
                    raise AppServerBlocked('summary does not match the streamed final candidate')
                _checked_recheck(recheck)
                candidate = _candidate(final[1])
                if candidate.outcome == 'success' and (command_event is None or command_event[1] != 0):
                    raise AppServerBlocked('success candidate lacks matching command completion')
                if candidate.outcome == 'success' and (
                        command_event[2] != sum(i*i for i in range(10000))
                        or candidate.reported_checksum != command_event[2]):
                    raise AppServerBlocked('command event and candidate checksum do not agree')
                _diagnostic_milestone('final_validated')
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
                if item['type'] == 'agentMessage' and (
                        item.get('phase') not in (None, 'commentary', 'final_answer')
                        or type(item.get('text')) is not str):
                    raise AppServerBlocked('malformed agent message phase or text')
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
                    if item['type'] == 'agentMessage' and item.get('phase') != 'commentary':
                        if item['text'].strip():
                            last_summary_item_id = item_id
                        # MessagePhase.ts requires compatibility for unknown
                        # phases. Only closed-schema canary JSON is eligible;
                        # ordinary unlabelled commentary is never evidence.
                        try:
                            _candidate(item['text'])
                        except AppServerBlocked:
                            if item.get('phase') == 'final_answer':
                                raise
                        else:
                            if final is not None:
                                raise AppServerBlocked('ambiguous multiple final candidates')
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
                _item_delta(method, params)
                continue  # Bound/count bytes, never assemble deltas as evidence.
            raise AppServerBlocked('unsupported app-server notification')
