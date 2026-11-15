"""Read-only goal change collection, deliberately disconnected from publishers.

Only call on a manager-owned, recovered workspace after the trusted supervisor
has reaped the worker AND all descendants and revoked their filesystem access,
or on a manager-owned atomic frozen copy whose bytes and ancestor directories
are inaccessible to worker writes, replacement and rename. The legacy argument
``worker_stopped=True`` asserts one of these trusted caller conditions; it is
not proof of process isolation. A frozen copy does not establish a consistent
point-in-time view of its live source or prove execution/research success. Private
inspection before restart never permits publication before restart is verified.
The baseline must be captured before dispatch from approved staged source. The
source SHA is a binding, not a claim that this module verified Git provenance.

No generated code is imported/executed. Inspection does not prove absence of
secrets. Every candidate remains unapproved: publication needs a separate trusted
content-review receipt bound to candidate_digest and its exact file manifest.
A plan execution approval is never that receipt. Do not feed these bytes into
the existing fixture-result publisher.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import os
from pathlib import Path
import re
import stat

from .schema import ContractError, canonical, relative_path, fields

MAX_BYTES = 1024 * 1024
HARD_MAX_BYTES = 16 * MAX_BYTES
MAX_ENTRIES = 4096


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _binding(source_sha, write_scope):
    if type(source_sha) is not str or re.fullmatch('[0-9a-f]{40}', source_sha) is None:
        raise ContractError('invalid goal source binding')
    if type(write_scope) not in (tuple, list) or not 1 <= len(write_scope) <= 8:
        raise ContractError('bounded goal write scope required')
    for path in write_scope:
        relative_path(path)
        if any(part.startswith('.') for part in path.split('/')) or any(c in path for c in '*?[]'):
            raise ContractError('unsafe goal scope')
    if len(set(write_scope)) != len(write_scope):
        raise ContractError('duplicate goal scope')
    return tuple(sorted(write_scope))


@dataclass(frozen=True)
class GoalEntry:
    path: str
    kind: str
    mode: int
    content: bytes

    def __post_init__(self):
        relative_path(self.path)
        if any(part.startswith('.') for part in self.path.split('/')):
            raise ContractError('hidden goal path prohibited')
        if (self.kind not in ('file', 'directory') or type(self.mode) is not int
                or not 0 <= self.mode <= 0o7777 or type(self.content) is not bytes
                or len(self.content) > HARD_MAX_BYTES or b'\x00' in self.content
                or (self.kind == 'directory' and self.content)):
            raise ContractError('invalid frozen goal entry')
        try:
            self.content.decode('utf-8')
        except UnicodeError:
            raise ContractError('invalid goal text') from None

    def to_dict(self):
        return dict(path=self.path, kind=self.kind, mode=self.mode,
                    content_base64=base64.b64encode(self.content).decode('ascii'))

    @classmethod
    def from_dict(cls, value):
        fields(value, ('path', 'kind', 'mode', 'content_base64'))
        try:
            raw = value['content_base64']
            if type(raw) is not str or len(raw) > HARD_MAX_BYTES * 2:
                raise ValueError()
            content = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            raise ContractError('invalid encoded goal entry') from None
        return cls(value['path'], value['kind'], value['mode'], content)

    def descriptor(self):
        return {'path': self.path, 'kind': self.kind, 'mode': self.mode,
                'size_bytes': len(self.content), 'sha256': _digest(self.content)}


@dataclass(frozen=True)
class GoalWorkspaceSnapshot:
    source_sha: str
    write_scope: tuple[str, ...]
    entries: tuple[GoalEntry, ...]
    max_file_bytes: int
    max_total_bytes: int
    inputs: tuple[str, ...] = ()

    @property
    def snapshot_digest(self):
        return _digest(canonical({'source_sha': self.source_sha,
                                 'write_scope': self.write_scope,
                                 'inputs': self.inputs,
                                 'max_file_bytes': self.max_file_bytes,
                                 'max_total_bytes': self.max_total_bytes,
                                 'entries': [e.descriptor() for e in self.entries]}))


@dataclass(frozen=True)
class GoalChange:
    path: str
    before: GoalEntry | None
    after: GoalEntry | None

    def descriptor(self):
        return {'path': self.path,
                'before': self.before.descriptor() if self.before else None,
                'after': self.after.descriptor() if self.after else None}


@dataclass(frozen=True)
class GoalChangeCandidate:
    source_sha: str
    write_scope: tuple[str, ...]
    baseline_digest: str
    final_digest: str
    changes: tuple[GoalChange, ...]

    def __post_init__(self):
        _binding(self.source_sha, self.write_scope)
        if type(self.write_scope) is not tuple or type(self.changes) is not tuple:
            raise ContractError('immutable goal candidate required')
        for value in (self.baseline_digest, self.final_digest):
            if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
                raise ContractError('invalid goal snapshot digest')
        names = []
        total = 0
        for change in self.changes:
            if type(change) is not GoalChange:
                raise ContractError('invalid goal change')
            relative_path(change.path)
            if not any(change.path == p or change.path.startswith(p + '/') for p in self.write_scope):
                raise ContractError('goal change outside approved scope')
            if change.before == change.after:
                raise ContractError('empty goal change')
            for entry in (change.before, change.after):
                if entry is not None:
                    if type(entry) is not GoalEntry or entry.path != change.path:
                        raise ContractError('goal change path mismatch')
                    total += len(entry.content)
            names.append(change.path)
        if names != sorted(set(names)) or len(names) > MAX_ENTRIES or total > 2 * HARD_MAX_BYTES:
            raise ContractError('goal change manifest outside bounds')

    def to_dict(self):
        return dict(source_sha=self.source_sha, write_scope=list(self.write_scope),
                    baseline_digest=self.baseline_digest, final_digest=self.final_digest,
                    changes=[dict(path=c.path, before=c.before.to_dict() if c.before else None,
                                  after=c.after.to_dict() if c.after else None) for c in self.changes])

    @classmethod
    def from_dict(cls, value):
        fields(value, ('source_sha', 'write_scope', 'baseline_digest', 'final_digest', 'changes'))
        if (type(value['changes']) is not list or len(value['changes']) > MAX_ENTRIES
                or type(value['write_scope']) is not list):
            raise ContractError('invalid serialized goal candidate')
        changes = []
        for change in value['changes']:
            fields(change, ('path', 'before', 'after'))
            changes.append(GoalChange(change['path'],
                GoalEntry.from_dict(change['before']) if change['before'] is not None else None,
                GoalEntry.from_dict(change['after']) if change['after'] is not None else None))
        return cls(value['source_sha'], tuple(value['write_scope']), value['baseline_digest'],
                   value['final_digest'], tuple(changes))

    @property
    def publish_authorized(self):
        return False

    @property
    def requires_content_review(self):
        return True

    @property
    def candidate_digest(self):
        return _digest(canonical({'kind': 'unreviewed-goal-changes-v1',
                                 'source_sha': self.source_sha,
                                 'write_scope': self.write_scope,
                                 'baseline_digest': self.baseline_digest,
                                 'final_digest': self.final_digest,
                                 'changes': [c.descriptor() for c in self.changes]}))


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _scan(root, max_file_bytes, max_total_bytes):
    """Descriptor-relative no-follow tree read, with bounded size and entry count."""
    entries = []
    total = 0

    def walk(fd, prefix):
        nonlocal total
        before_dir = os.fstat(fd)
        names = sorted(os.listdir(fd))
        if len(names) + len(entries) > MAX_ENTRIES:
            raise ContractError('goal workspace entry limit exceeded')
        for name in names:
            path = prefix + name
            relative_path(path)
            if any(part.startswith('.') for part in path.split('/')):
                raise ContractError('hidden goal workspace path prohibited')
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            mode = stat.S_IMODE(before.st_mode)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(before) != _identity(os.fstat(child)):
                        raise ContractError('goal directory changed during read')
                    entries.append(GoalEntry(path, 'directory', mode, b''))
                    walk(child, path + '/')
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                if before.st_size > min(max_file_bytes, max_total_bytes - total):
                    raise ContractError('goal workspace size limit exceeded')
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    if _identity(before) != _identity(os.fstat(child)):
                        raise ContractError('goal file changed during open')
                    chunks = []
                    remaining = min(max_file_bytes, max_total_bytes - total) + 1
                    while remaining:
                        data = os.read(child, min(remaining, 65536))
                        if not data:
                            break
                        chunks.append(data)
                        remaining -= len(data)
                    content = b''.join(chunks)
                    if (len(content) != before.st_size
                            or _identity(before) != _identity(os.fstat(child))):
                        raise ContractError('goal file changed during read')
                    content.decode('utf-8', errors='strict')
                    if b'\x00' in content:
                        raise ContractError('binary goal content prohibited')
                    total += len(content)
                    entries.append(GoalEntry(path, 'file', mode, content))
                finally:
                    os.close(child)
            else:
                raise ContractError('goal symlink, hardlink or special file prohibited')
            if len(entries) > MAX_ENTRIES:
                raise ContractError('goal workspace entry limit exceeded')
            if _identity(before) != _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                raise ContractError('goal entry changed during read')
        if names != sorted(os.listdir(fd)) or _identity(before_dir) != _identity(os.fstat(fd)):
            raise ContractError('goal directory changed during read')

    descriptors = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(fd)
        for part in Path(root).absolute().parts[1:]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        walk(fd, '')
        return tuple(sorted(entries, key=lambda entry: entry.path))
    except (OSError, UnicodeError, RecursionError):
        raise ContractError('goal workspace cannot be safely collected') from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def snapshot_goal_workspace(root, *, source_sha, write_scope, worker_stopped,
                            max_file_bytes=MAX_BYTES, max_total_bytes=MAX_BYTES, inputs=()):
    """Freeze trusted stopped workspaces or protected copies; this does not stop jobs.

    Scope entries cover the exact path and its descendants. Two identical scans
    detect ordinary concurrent changes; they do not replace the supervisor's
    stop/revoke guarantee against an adversarial active writer.
    """
    scope = _binding(source_sha, write_scope)
    if (type(inputs) not in (tuple, list) or len(inputs) > 32
            or any(type(p) is not str for p in inputs) or len(set(inputs)) != len(inputs)):
        raise ContractError('invalid goal input binding')
    for path in inputs:
        relative_path(path)
    inputs = tuple(inputs)
    if worker_stopped is not True:
        raise ContractError('trusted worker shutdown or protected atomic copy required')
    if any(type(n) is not int or not 1 <= n <= HARD_MAX_BYTES
           for n in (max_file_bytes, max_total_bytes)):
        raise ContractError('invalid goal collection byte limit')
    entries = _scan(root, max_file_bytes, max_total_bytes)
    if entries != _scan(root, max_file_bytes, max_total_bytes):
        raise ContractError('goal workspace changed between scans')
    return GoalWorkspaceSnapshot(source_sha, scope, entries, max_file_bytes, max_total_bytes, inputs)


def collect_goal_changes(root, baseline, *, source_sha, write_scope, worker_stopped, inputs=()):
    """Reject out-of-scope changes, including deletions and directory metadata."""
    scope = _binding(source_sha, write_scope)
    if (type(baseline) is not GoalWorkspaceSnapshot or baseline.source_sha != source_sha
            or baseline.write_scope != scope or baseline.inputs != tuple(inputs)):
        raise ContractError('goal baseline source or scope mismatch')
    final = snapshot_goal_workspace(root, source_sha=source_sha, write_scope=scope,
                                    worker_stopped=worker_stopped,
                                    max_file_bytes=baseline.max_file_bytes,
                                    max_total_bytes=baseline.max_total_bytes, inputs=inputs)
    before = {e.path: e for e in baseline.entries}
    after = {e.path: e for e in final.entries}
    changes = []
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) == after.get(path):
            continue
        if not any(path == item or path.startswith(item + '/') for item in scope):
            raise ContractError('goal workspace changed outside approved write scope')
        changes.append(GoalChange(path, before.get(path), after.get(path)))
    return GoalChangeCandidate(source_sha, scope, baseline.snapshot_digest,
                               final.snapshot_digest, tuple(changes))
