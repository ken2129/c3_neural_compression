"""Fail-closed Codex CLI adapter for one fixed CPU canary, not research jobs.

No production supervisor is supplied by this module. Help/version inspection
and the explicit, model-free sandbox probe are the only direct subprocesses.
Live execution is disabled by default and requires a trusted-code supervisor
which independently enforces admission, credentials, cost, resources, lifetime,
and descendant cleanup. A CLI version, probe result, UID, or operator boolean
does not establish that authority. Never construct a supervisor from Issue JSON.

CLI/source references checked 2026-10-17:
https://learn.chatgpt.com/docs/non-interactive-mode
https://learn.chatgpt.com/docs/developer-commands?surface=cli
https://github.com/openai/codex/blob/ce0323f2f2d3b25e82806cd555237da3db4e1897/codex-rs/linux-sandbox/README.md
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import json
import re
import subprocess
from pathlib import PurePosixPath
from typing import Callable

from research_harness.schema import ContractError, canonical, loads


MAX_CAPTURE_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 65536
INSPECT_TIMEOUT = 5
PROBE_TIMEOUT = 10
WORKER_SCHEMA = {
    'type': 'object',
    'properties': {
        'schema_version': {'type': 'string', 'const': 'c3-cpu-canary-worker-v1'},
        'outcome': {'type': 'string', 'enum': ['success', 'failure']},
        'reported_checksum': {'type': ['integer', 'null']},
    },
    'required': ['schema_version', 'outcome', 'reported_checksum'],
    'additionalProperties': False,
}
CANARY_PROMPT = (
    'Perform only this bounded CPU canary. Do not inspect project files, use the '
    'network, install anything, create background processes, or change files. '
    'Run python3 -I -S -c "print(sum(i*i for i in range(10000)))" once using '
    'the normal sandbox. If it is blocked, report failure; do not retry outside '
    'the sandbox. Return the required JSON with schema_version '
    'c3-cpu-canary-worker-v1, outcome success or failure, and reported_checksum '
    'equal to the observed integer on success or null on failure. '
    'Your report is an untrusted candidate and will be independently checked.'
)
PROBE_CODE = "print('c3-normal-sandbox-started-v1')"
# Prepared by the protected launcher with dummy contents, never credentials.
# /work/canary is the only workspace; the outside sentinel is worker-owned and
# writable before sandboxing. The control sentinel belongs to UID10001.
BOUNDARY_PROBE_CODE = r'''
import errno, json, os, socket
allowed = '/work/canary/.c3-probe-write'
outside = '/work/sandbox-outside/c3-dummy'
control = '/control/c3-dummy'
checks = {}
with open(outside, 'rb') as source:
    checks['outside_sentinel_present'] = source.read(32) == b'BOUNDARY-OUTSIDE\n'
fd = os.open(allowed, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
os.write(fd, b'canary\n'); os.close(fd); os.unlink(allowed)
checks['workspace_write'] = True
def denied(fn):
    try:
        obj = fn()
        if isinstance(obj, int): os.close(obj)
        else: obj.close()
        return False
    except OSError as exc:
        return exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
checks['outside_write_denied'] = denied(lambda: os.open(outside, os.O_WRONLY|os.O_NOFOLLOW))
checks['control_read_denied'] = denied(lambda: os.open(control, os.O_RDONLY|os.O_NOFOLLOW))
checks['network_socket_denied'] = denied(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
print(json.dumps(checks, sort_keys=True))
raise SystemExit(0 if all(checks.values()) else 125)
'''
REQUIRED_EXEC_OPTIONS = (
    '--json', '--ephemeral', '--ignore-user-config', '--strict-config',
    '--sandbox', '--model', '--output-schema', '--cd', '--config',
)
CONFIG = (
    'approval_policy="never"',
    'web_search="disabled"',
    'sandbox_workspace_write.network_access=false',
    'sandbox_workspace_write.exclude_slash_tmp=true',
    'sandbox_workspace_write.exclude_tmpdir_env_var=true',
    'shell_environment_policy.inherit="none"',
    'shell_environment_policy.ignore_default_excludes=false',
    'shell_environment_policy.experimental_use_profile=false',
    'shell_environment_policy.set={PATH="/usr/bin:/bin",LANG="C.UTF-8"}',
    'features.hooks=false',
    'features.multi_agent=false',
    'features.memories=false',
    'features.goals=false',
    'project_doc_max_bytes=0',
)


class CodexBlocked(ContractError):
    """A missing boundary is a blocked run, never permission to weaken it."""


@dataclass(frozen=True)
class CliInspection:
    version: str | None
    expected_version: str
    supported: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class SandboxInspection:
    status: str
    # A positive start probe is deliberately narrower than boundary validation.
    boundary_validated: bool = False


@dataclass(frozen=True)
class Preflight:
    execution_allowed: bool
    mode: str
    cli: CliInspection | None
    sandbox: SandboxInspection | None
    blockers: tuple[str, ...]

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CanaryRequest:
    argv: tuple[str, ...]
    stdin: bytes
    cwd: str
    schema_path: str
    schema_bytes: bytes
    # This is a deadline request to the supervisor, not enforced by this object.
    wall_seconds: int = 120
    max_capture_bytes: int = MAX_CAPTURE_BYTES


@dataclass(frozen=True)
class CanaryCandidate:
    outcome: str
    reported_checksum: int | None
    # Always false. Protected controller recomputes and binds the final artifact.
    independently_validated: bool = False


@dataclass(frozen=True)
class BoundaryProbeRequest:
    argv: tuple[str, ...]
    cwd: str = '/work/canary'
    wall_seconds: int = 10
    max_capture_bytes: int = 4096


class ValidatedSupervisor(ABC):
    """Trusted application-code boundary; there is no implementation here.

    admit_and_execute MUST atomically recheck approval/fencing and immutable
    source/config; run the normal sandbox and negative boundary probes under
    the actual worker UID/mounts/seccomp; enforce aggregate memory/CPU/pids/disk
    and an independent wall watchdog; isolate credentials and account spending;
    capture bounded output; terminate ALL descendants including new sessions;
    and prove the allocation empty before returning. For API billing, require
    either a separately enforced cap or explicit owner approval of the known
    uncapped-billing risk for this one bounded canary. Codex usage telemetry and
    wall time are not billing caps. Missing/unverifiable gates
    MUST raise CodexBlocked. It must preserve any stricter managed policy.

    An isolated per-attempt container/cgroup with an external owner is suitable;
    the fixture ProcessManager, same-UID receipts, process-group scans, RLIMITs,
    elapsed time, and CLI usage telemetry do not satisfy this contract.
    Instantiating a subclass is not itself evidence. Production integration
    requires an independently reviewed implementation and adversarial tests.
    """

    @abstractmethod
    def admit_and_execute(self, request: CanaryRequest) -> subprocess.CompletedProcess:
        """Return only after execution and complete allocation cleanup are proved."""
        raise NotImplementedError


def _path(value: str) -> str:
    if (type(value) is not str or not value.startswith('/') or value == '/'
            or any(ord(c) < 32 for c in value) or '\\' in value
            or any(p in ('', '.', '..') for p in value.split('/')[1:])):
        raise ContractError('expected a normalized absolute runtime path')
    return str(PurePosixPath(value))


def _bytes(value) -> bytes:
    if type(value) is str:
        value = value.encode('utf-8')
    if type(value) is not bytes or len(value) > MAX_CAPTURE_BYTES:
        raise CodexBlocked('CLI output missing or exceeds capture limit')
    return value


class CodexAdapter:
    """Build the real CLI invocation; never silently substitute a fake worker.

    binary, expected_version, paths, model and runner are trusted application
    configuration, never Issue fields. Pin the binary/image digest in deployment
    as well as the exact version; help text alone is not binary attestation.
    """

    def __init__(self, binary: str, *, expected_version: str = '0.160.0',
                 enabled: bool = False, runner: Callable = subprocess.run):
        self.binary = _path(binary)
        if type(expected_version) is not str or not re.fullmatch(r'0\.(159|160)\.\d+', expected_version):
            raise ContractError('only an explicitly pinned 0.159/0.160 CLI is supported')
        if type(enabled) is not bool or not callable(runner):
            raise ContractError('invalid trusted adapter configuration')
        self.expected_version = expected_version
        self.enabled = enabled
        self._runner = runner

    def _inspect(self, args):
        # No ambient credentials, model request, login, shell, or user config.
        result = self._runner(
            [self.binary, *args], stdin=subprocess.DEVNULL,
            capture_output=True, check=False, timeout=INSPECT_TIMEOUT,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                 'HOME': '/nonexistent', 'CODEX_HOME': '/nonexistent'},
            close_fds=True, shell=False,
        )
        out = _bytes(result.stdout)
        _bytes(result.stderr)
        if result.returncode != 0:
            raise CodexBlocked('CLI inspection failed')
        return out.decode('utf-8', errors='strict')

    def inspect_cli(self) -> CliInspection:
        """Run only version/help commands; never inspect or create credentials."""
        version = None
        try:
            text = self._inspect(['--version']).strip()
            match = re.fullmatch(r'codex-cli (0\.[0-9]+\.[0-9]+)', text)
            version = match[1] if match else None
            if version != self.expected_version:
                return CliInspection(version, self.expected_version, False, ('CLI_VERSION_MISMATCH',))
            help_text = self._inspect(['exec', '--help'])
            missing = tuple(option for option in REQUIRED_EXEC_OPTIONS if option not in help_text)
            if missing:
                return CliInspection(version, self.expected_version, False, ('CLI_OPTIONS_UNSUPPORTED',))
            sandbox_help = self._inspect(['sandbox', '--help'])
            # New CLI selects the OS. Reject legacy/nested syntax, do not guess.
            if ('[COMMAND]' not in sandbox_help or '--config' not in sandbox_help
                    or re.search(r'^Commands:', sandbox_help, re.MULTILINE)):
                return CliInspection(version, self.expected_version, False, ('SANDBOX_CLI_UNSUPPORTED',))
            return CliInspection(version, self.expected_version, True, ())
        except (OSError, subprocess.SubprocessError, UnicodeError, ContractError):
            return CliInspection(version, self.expected_version, False, ('CLI_INSPECTION_FAILED',))

    def _require_cli(self, inspection: CliInspection):
        if (type(inspection) is not CliInspection or not inspection.supported
                or inspection.version != self.expected_version
                or inspection.expected_version != self.expected_version or inspection.blockers):
            raise CodexBlocked('exact CLI version and supported options must be inspected')

    def probe_sandbox(self, *, inspection: CliInspection, workspace: str,
                      codex_home: str, python: str = '/usr/bin/python3') -> SandboxInspection:
        """Explicit offline start probe, in the calling UID/runtime only.

        Caller must supply an isolated fresh CODEX_HOME and a clean fixed
        workspace; this never creates them, signs in, or changes system policy.
        Success is NOT validation of write, network, secret or process isolation.
        A supervisor must repeat this and adversarial probes in the real worker
        allocation immediately before launch. No automatic fallback is allowed.
        """
        self._require_cli(inspection)
        workspace, codex_home, python = map(_path, (workspace, codex_home, python))
        argv = [self.binary, 'sandbox', '-c', 'sandbox_mode="workspace-write"',
                '-c', 'sandbox_workspace_write.network_access=false',
                '-c', 'sandbox_workspace_write.exclude_slash_tmp=true',
                '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=true',
                '--', python, '-I', '-S', '-c', PROBE_CODE]
        try:
            result = self._runner(
                argv, cwd=workspace, stdin=subprocess.DEVNULL,
                capture_output=True, check=False, timeout=PROBE_TIMEOUT,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                     'HOME': codex_home, 'CODEX_HOME': codex_home},
                close_fds=True, shell=False,
            )
            stdout, stderr = _bytes(result.stdout), _bytes(result.stderr)
            if result.returncode == 0 and stdout == b'c3-normal-sandbox-started-v1\n':
                return SandboxInspection('STARTED_ONLY')
            return SandboxInspection('FAILED')
        except (OSError, subprocess.SubprocessError, ContractError):
            return SandboxInspection('FAILED')

    def preflight(self, *, inspection: CliInspection | None = None,
                  sandbox: SandboxInspection | None = None) -> Preflight:
        """Check-only report. Cannot be promoted to admission by editing JSON."""
        blockers = []
        if not self.enabled:
            blockers.append('ADAPTER_DISABLED')
        if inspection is None:
            blockers.append('CLI_NOT_INSPECTED')
        else:
            try:
                self._require_cli(inspection)
            except CodexBlocked:
                blockers.extend((inspection.blockers or ('CLI_INSPECTION_INVALID',))
                                if type(inspection) is CliInspection else ('CLI_INSPECTION_INVALID',))
        if sandbox is None:
            blockers.append('NORMAL_SANDBOX_NOT_PROBED')
        elif type(sandbox) is not SandboxInspection or sandbox.status != 'STARTED_ONLY':
            blockers.append('NORMAL_SANDBOX_PROBE_FAILED')
        blockers.extend((
            'WORKER_BOUNDARY_NOT_VALIDATED', 'PROTECTED_SUPERVISOR_NOT_IMPLEMENTED',
            'DESCENDANT_CLEANUP_NOT_ENFORCED', 'HARD_RESOURCES_NOT_ENFORCED',
            'INDEPENDENT_DEADLINE_NOT_ENFORCED', 'SPENDING_CAP_OR_RISK_APPROVAL_MISSING',
            'CREDENTIAL_ISOLATION_NOT_VALIDATED', 'APPROVAL_FENCE_NOT_BOUND',
        ))
        return Preflight(False, 'check-only', inspection, sandbox, tuple(blockers))

    def build_boundary_probe(self, *, inspection: CliInspection) -> BoundaryProbeRequest:
        """Build a fixed model-free positive/negative sandbox test.

        Execute only through the protected supervisor in the real worker UID,
        namespace, config and mounts. Provision fresh dummy sentinels first;
        ENOENT is not a permission denial. This method runs nothing and cannot
        promote an unbound returned JSON blob into execution authority.
        """
        self._require_cli(inspection)
        argv = [self.binary, 'sandbox', '-c', 'sandbox_mode="workspace-write"']
        for option in CONFIG:
            argv.extend(['-c', option])
        argv.extend(['--', '/usr/bin/python3', '-I', '-S', '-c', BOUNDARY_PROBE_CODE])
        return BoundaryProbeRequest(tuple(argv))

    def build_canary_request(self, *, inspection: CliInspection, workspace: str,
                             schema_path: str, model: str) -> CanaryRequest:
        self._require_cli(inspection)
        workspace, schema_path = map(_path, (workspace, schema_path))
        if (type(model) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', model)
                or model.endswith('-latest')):
            raise ContractError('an explicit trusted model identifier is required')
        argv = [self.binary, 'exec', '--json', '--ephemeral', '--ignore-user-config',
                '--strict-config', '--sandbox', 'workspace-write', '--model', model,
                '--cd', workspace, '--output-schema', schema_path]
        for option in CONFIG:
            argv.extend(['-c', option])
        argv.append('-')
        return CanaryRequest(tuple(argv), CANARY_PROMPT.encode('utf-8'), workspace,
                             schema_path, canonical(WORKER_SCHEMA))

    def run_canary(self, *, inspection: CliInspection, workspace: str,
                   schema_path: str, model: str,
                   supervisor: ValidatedSupervisor | None = None) -> CanaryCandidate:
        """Delegate real exec only to the independently implemented authority.

        This module never calls Popen/run on the model invocation, copies auth,
        broadens permissions, or treats preflight output as execution approval.
        """
        if not self.enabled:
            raise CodexBlocked('ADAPTER_DISABLED')
        if not isinstance(supervisor, ValidatedSupervisor):
            raise CodexBlocked('PROTECTED_SUPERVISOR_NOT_IMPLEMENTED')
        request = self.build_canary_request(inspection=inspection, workspace=workspace,
                                            schema_path=schema_path, model=model)
        result = supervisor.admit_and_execute(request)
        if not isinstance(result, subprocess.CompletedProcess) or tuple(result.args) != request.argv:
            raise CodexBlocked('supervisor returned an invalid execution result')
        return parse_candidate(result)


def parse_candidate(result: subprocess.CompletedProcess) -> CanaryCandidate:
    """Validate CLI framing/JSON shape only, never scientific truth or cleanup."""
    stdout, stderr = _bytes(result.stdout), _bytes(result.stderr)
    if result.returncode != 0:
        raise CodexBlocked('Codex canary failed')
    completed = 0
    last_message = None
    try:
        for line in stdout.splitlines():
            if len(line) > MAX_EVENT_BYTES:
                raise ContractError('oversized CLI event')
            event = loads(line.decode('utf-8'))
            if type(event) is not dict or type(event.get('type')) is not str:
                raise ContractError('invalid CLI event')
            kind = event['type']
            if kind in ('error', 'turn.failed'):
                raise ContractError('CLI reported failure')
            if kind == 'turn.completed':
                completed += 1
            if kind == 'item.completed':
                item = event.get('item')
                if type(item) is dict and item.get('type') == 'agent_message':
                    last_message = item.get('text')
        if completed != 1 or type(last_message) is not str:
            raise ContractError('missing or ambiguous completed turn')
        value = loads(last_message)
        if (type(value) is not dict
                or set(value) != {'schema_version', 'outcome', 'reported_checksum'}
                or value['schema_version'] != 'c3-cpu-canary-worker-v1'
                or value['outcome'] not in ('success', 'failure')):
            raise ContractError('invalid canary candidate')
        checksum = value['reported_checksum']
        if value['outcome'] == 'success':
            if type(checksum) is not int or not 0 <= checksum < 2**63:
                raise ContractError('invalid reported checksum')
        elif checksum is not None:
            raise ContractError('failure cannot claim a checksum')
        return CanaryCandidate(value['outcome'], checksum)
    except (ContractError, UnicodeError, TypeError, ValueError) as exc:
        raise CodexBlocked('invalid or failed Codex output') from exc
