"""One-shot publication-only recovery of saved Issue 77; no execution path.

Docker facts are an operator-collected, protected preparation record, not an
in-container proof. The operator must keep the old container stopped with
restart disabled and prevent all other volume users for this entire pass.
The original service ledger is never updated; a separate journal owns writes.
"""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import time

from .issue_intake import content_hash, timestamp
from .owner_approval import OWNER_ID, OWNER_LOGIN, approval_read_budget
from .persistent_fixture import private, exclusive_record, sync_directory
from .result_publisher import (GitHubResultPublisher, ModelPublicationRequest, PublicationState,
                               parse_publication_request)
from .schema import ContractError, REPOSITORY, canonical, fields, loads
from .service_state import ServiceState

ISSUE = 77
SCHEMA = 'c3-publication-only-approval-v1'
ENTRYPOINT = ['python', '-I', '-B', '/opt/service/recover_publication.py']
MAX_RECORD = 1024 * 1024


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _hex(value, size):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{%d}' % size, value):
        raise ContractError('invalid recovery identity')
    return value


def validate_fence(record, *, old_container_id, original_source_sha, source_sha, now):
    """Validate authentic external observations; never claim to collect them."""
    try:
        fields(record, ('schema', 'mode', 'source_sha', 'observed_at', 'expires_at',
                        'old', 'recovery', 'consumers', 'volume'))
        if record['schema'] != 'c3-publication-recovery-deployment-v1': raise ValueError()
        if record['mode'] not in ('prepare', 'publish'): raise ValueError()
        if record['source_sha'] != _hex(source_sha, 40): raise ValueError()
        observed, expires = timestamp(record['observed_at']), timestamp(record['expires_at'])
        if not observed <= now < expires <= observed + 3600: raise ValueError()
        old, recovery, volume = record['old'], record['recovery'], record['volume']
        old_id, new_id = _hex(old['Id'], 64), _hex(recovery['Id'], 64)
        if old_id != old_container_id or old_id == new_id: raise ValueError()
        if (old['Config']['Labels'].get('c3.purpose') != 'dedicated-research-controller'
                or old['Config']['Labels'].get('c3.source-sha') != original_source_sha
                or old['Config']['Hostname'] != old_id[:12]): raise ValueError()
        if type(recovery['Image']) is not str or not re.fullmatch('sha256:[0-9a-f]{64}', recovery['Image']):
            raise ValueError()
        state = old['State']
        if (state['Status'] != 'exited' or state['Running'] is not False
                or state['Restarting'] is not False or state['Paused'] is not False
                or state['Dead'] is not False or type(state['Pid']) is not int or state['Pid'] != 0
                or old['HostConfig']['RestartPolicy']['Name'] != 'no'): raise ValueError()
        config, host = recovery['Config'], recovery['HostConfig']
        if (config['User'] != '10001:10001' or config['Entrypoint'] != ENTRYPOINT
                or config['Cmd'] != ['--once']
                or config['Hostname'] != new_id[:12]
                or config['Labels'].get('c3.purpose') != 'publication-only-recovery'
                or config['Labels'].get('c3.source-sha') != source_sha): raise ValueError()
        if (host['RestartPolicy']['Name'] != 'no' or host['Privileged'] is not False
                or host['ReadonlyRootfs'] is not True or host['PidMode'] != ''
                or host['Init'] is not False
                or host['IpcMode'] not in ('private', 'none') or host['CgroupnsMode'] != 'private'
                or host['NetworkMode'] != 'bridge' or host.get('CapAdd') not in (None, [])
                or host['CapDrop'] not in (['ALL'], ['CAP_ALL'])
                or host['SecurityOpt'] != ['no-new-privileges:true'] or host['Runtime'] != 'runc'
                or host.get('Devices') not in (None, []) or host.get('DeviceRequests') not in (None, [])
                or host.get('DeviceCgroupRules') not in (None, []) or host.get('Tmpfs') not in (None, {})):
            raise ValueError()
        for key, maximum in (('Memory', 1024**3), ('PidsLimit', 64), ('NanoCpus', 10**9)):
            if type(host[key]) is not int or not 0 < host[key] <= maximum: raise ValueError()
        if type(host['MemorySwap']) is not int or host['MemorySwap'] != host['Memory']: raise ValueError()
        name = volume['Name']
        if (type(name) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
                or volume['Driver'] != 'local' or volume['Scope'] != 'local'
                or volume.get('Options') not in (None, {})): raise ValueError()
        if record['consumers'] != sorted([old_id, new_id]): raise ValueError()
        if host.get('Binds') not in (None, [], [name+':/control'], [name+':/control:rw']): raise ValueError()
        for container in (old, recovery):
            mounts = container['Mounts']
            if type(mounts) is not list or len(mounts) != 1: raise ValueError()
            mount = mounts[0]
            if (mount['Type'] != 'volume' or mount['Name'] != name
                    or mount['Destination'] != '/control' or mount['RW'] is not True
                    or type(volume['Mountpoint']) is not str or not volume['Mountpoint'].startswith('/')
                    or mount['Source'] != volume['Mountpoint']): raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ContractError('publication recovery deployment facts do not match') from None
    return new_id


def _original_files(root):
    private(root, True)
    private(root / 'claims', True)
    private(root / 'manager.lock')
    lock = (root / 'manager.lock').lstat()
    answer = {}
    answer['manager.lock'] = digest(dict(device=lock.st_dev, inode=lock.st_ino))
    for name in ('state.sqlite', 'initialized.json', 'claims/77.json'):
        path = root / name
        private(path)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ContractError('original state exceeds recovery bound')
        answer[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return answer


def _saved(state):
    saved = state.get(ISSUE)
    if saved is None or saved['phase'] != 'RESULT_READY' or saved['publication'] is None:
        raise ContractError('saved result is not eligible for publication recovery')
    original = parse_publication_request(saved['result'])
    if (type(original) is not ModelPublicationRequest
            or original.base_sha != original.result.source_sha
            or original.base_sha != saved['binding']['source_sha']):
        raise ContractError('original Issue 77 result/source binding changed')
    publication = PublicationState.from_dict(saved['publication'])
    if (publication.phase != 'BLOCKED' or publication.reason != 'BASE_CHANGED'
            or publication.head_sha is not None or publication.pr_number is not None):
        raise ContractError('original publication may have written or is not base-blocked')
    row = state.db.execute('SELECT before_epoch,after_epoch FROM service_epochs WHERE issue=?', (ISSUE,)).fetchone()
    if row is None or row[1] is None: raise ContractError('confirmed restart metadata required')
    # ServiceState already validated the complete stored before/after boundary.
    return saved, original, loads(row[1])['container_id']


def _receipt(comments, expected, now):
    matches = []
    if type(comments) is not list: raise ContractError('publication comments unavailable')
    for comment in comments:
        if type(comment) is not dict: raise ContractError('invalid publication comment metadata')
        author = comment.get('author')
        if type(author) is not dict or author.get('databaseId') != OWNER_ID or author.get('login') != OWNER_LOGIN:
            continue
        try: body = loads(comment['body'])
        except (ContractError, KeyError, TypeError, RecursionError): continue
        if type(body) is not dict or body.get('schema') != SCHEMA or body.get('issue') != ISSUE: continue
        fields(body, (*expected, 'decision', 'expires_at'))
        try:
            if any(body[key] != value for key, value in expected.items()): raise ValueError()
            if body['decision'] != 'approve-publication-only': raise ValueError()
            created = timestamp(comment['createdAt'])
            expires = timestamp(body['expires_at'])
            if (not created <= now < expires <= created + 3600
                    or comment['updatedAt'] != comment['createdAt']
                    or comment['lastEditedAt'] is not None or comment['editor'] is not None
                    or comment['isMinimized'] is not False
                    or type(comment['id']) is not str or not 1 <= len(comment['id']) <= 200): raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ContractError('publication-only approval is invalid or revoked') from None
        matches.append(dict(comment_id=comment['id'], body_sha256=digest(body)))
    if len(matches) != 1: raise ContractError('one fresh unedited owner publication-only approval required')
    return matches[0]


class _Journal:
    """Fixed single recovery journal; partial replacement remains fail-closed."""
    def __init__(self, root, binding, receipt=None):
        self.root = Path(root)
        private(self.root.parent, True)
        if not self.root.exists():
            self.root.mkdir(mode=0o700)
            sync_directory(self.root.parent)
        private(self.root, True)
        names = {p.name for p in self.root.iterdir()}
        if names not in (set(), {'journal.json'}):
            raise ContractError('publication recovery journal needs reconciliation')
        self.path = self.root / 'journal.json'
        if names:
            private(self.path)
            if self.path.stat().st_size > MAX_RECORD: raise ContractError('recovery journal exceeds bound')
            self.record = loads(self.path.read_bytes())
            fields(self.record, ('schema', 'binding', 'receipt', 'publication'))
            if (type(self.record['schema']) is not int or self.record['schema'] != 1
                    or self.record['binding'] != binding
                    or (receipt is not None and self.record['receipt'] != receipt)):
                raise ContractError('recovery journal binding changed')
        else:
            if receipt is None: raise ContractError('fresh publication approval required for new journal')
            request = parse_publication_request(binding['request'])
            self.record = dict(schema=1, binding=binding, receipt=receipt,
                publication=PublicationState(request.digest, request.branch).to_dict())
            exclusive_record(self.path, self.record)
        fields(self.record['receipt'], ('comment_id', 'body_sha256'))
        if (type(self.record['receipt']['comment_id']) is not str
                or not 1 <= len(self.record['receipt']['comment_id']) <= 200):
            raise ContractError('invalid saved publication receipt')
        _hex(self.record['receipt']['body_sha256'], 64)
        self.state = PublicationState.from_dict(self.record['publication'])

    def save(self, state):
        record = dict(self.record, publication=state.to_dict())
        temporary = self.root / 'pending.json'
        exclusive_record(temporary, record)
        os.replace(temporary, self.path)
        sync_directory(self.root)
        self.record, self.state = record, state


def recover_publication(root, journal_root, *, fence, source_sha, transport,
                        issue_client, comments_client, publish=False, clock=time.time):
    """Fixed Issue 77 only. Dependencies are trusted construction, not Issue data."""
    if type(publish) is not bool: raise ContractError('explicit recovery mode required')
    if type(fence) is not dict or fence.get('mode') != ('publish' if publish else 'prepare'):
        raise ContractError('protected publication recovery mode does not match')
    root = Path(root)
    if Path(journal_root) != root.parent / 'publication-recovery':
        raise ContractError('fixed publication recovery journal path required')
    original_files = _original_files(root)
    state = None
    try:
        state = ServiceState(root, require_epochs=True, read_only=True)
        if _original_files(root) != original_files:
            raise ContractError('original state changed while acquiring recovery lock')
        saved, original, old_id = _saved(state)
        validate_fence(fence, old_container_id=old_id, original_source_sha=original.result.source_sha,
                       source_sha=source_sha, now=clock())
        current = _hex(transport.base_sha(), 40)
        if current == original.base_sha: raise ContractError('publication base did not change')
        request = replace(original, base_sha=current)
        if transport.branch_sha(original) is not None or transport.find_pr(original) is not None:
            raise ContractError('original remote publication requires reconciliation')
        binding = dict(original_request_digest=original.digest, claim_digest=digest(saved['binding']),
                       original_files=original_files, source_sha=source_sha, request=request.to_dict(),
                       recovery_image_id=fence['recovery']['Image'])
        expected = dict(schema=SCHEMA, repository=REPOSITORY, issue=ISSUE,
            original_request_digest=original.digest, new_request_digest=request.digest,
            claim_digest=binding['claim_digest'], issue_content_sha256=original.issue_content_sha256,
            result_artifact_sha256=original.result.artifact_sha256,
            result_source_sha=original.result.source_sha, target_ref='main',
            new_base_sha=current, recovery_source_sha=source_sha,
            recovery_image_id=fence['recovery']['Image'])
        receipt = None
        def authorize():
            validate_fence(fence, old_container_id=old_id, original_source_sha=original.result.source_sha,
                           source_sha=source_sha, now=clock())
            if _original_files(root) != original_files:
                raise ContractError('original state changed during recovery')
            with approval_read_budget():
                before = issue_client.get_issue(ISSUE)
                comments = comments_client.comments(ISSUE)
                after = issue_client.get_issue(ISSUE)
            if (before != after or before['number'] != ISSUE or before['is_pr'] is not False
                    or before['state'] != 'open' or before['author_id'] != OWNER_ID
                    or content_hash(before) != original.issue_content_sha256):
                raise ContractError('original Issue changed or is unavailable')
            current_receipt = _receipt(comments, expected, clock())
            if receipt is not None and current_receipt != receipt:
                raise ContractError('publication approval changed during recovery')
            return current_receipt
        summary = dict(issue=ISSUE, original_request_digest=original.digest,
                       new_request_digest=request.digest, old_base_sha=original.base_sha,
                       new_base_sha=current, result_source_sha=original.result.source_sha,
                       recovery_source_sha=source_sha, original_files_unchanged=True,
                       result={key: getattr(original.result, key) for key in (
                           'reported_outcome', 'reported_checksum', 'command_observed',
                           'elapsed_seconds', 'model', 'artifact_sha256', 'status',
                           'research_success_verified')})
        if not publish:
            summary['phase'] = 'PREPARED_PUBLICATION_ONLY'
            summary['approval_template'] = dict(expected, decision='approve-publication-only',
                expires_at=datetime.fromtimestamp(clock()+3600, timezone.utc).isoformat().replace('+00:00', 'Z'))
            return summary
        if not Path(journal_root).exists() or not (Path(journal_root)/'journal.json').exists():
            receipt = authorize()
            if transport.branch_sha(request) is not None or transport.find_pr(request) is not None:
                raise ContractError('unjournaled recovery publication requires reconciliation')
        journal = _Journal(journal_root, binding, receipt)
        receipt = journal.record['receipt']
        publisher = GitHubResultPublisher(transport, persist_state=journal.save)
        result = publisher.publish(request, journal.state, authorized=True,
            authorize_callback=lambda: authorize() == receipt)
        summary.update(phase=result.phase, reason=result.reason, url=result.url)
        return summary
    finally:
        try:
            if _original_files(root) != original_files:
                raise ContractError('original state changed during recovery')
        finally:
            if state is not None: state.close()


def read_protected_fence(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o644 or not 0 < info.st_size <= MAX_RECORD):
        raise ContractError('protected operator preparation record required')
    return loads(path.read_bytes())
