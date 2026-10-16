"""SQLite fixture ledger, not an issuer or an OS security boundary.

Every read-modify-write operation uses BEGIN IMMEDIATE. The fake issuer identifier
is deliberately *not* authentication: these APIs are local CPU test fixtures only.
A lease timeout fences the old owner but never proves its process has stopped.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

from .schema import ContractError, Plan, canonical, identifier


class LedgerError(ContractError):
    pass


class AlreadyClaimed(LedgerError):
    pass


class StaleFence(LedgerError):
    pass


class ResourceBusy(LedgerError):
    pass


class InvalidTransition(LedgerError):
    pass


@dataclass(frozen=True)
class Claim:
    run_id: str
    attempt_id: str
    fence: int


def _number(value, *, positive=False):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise LedgerError('finite nonnegative number required')
    if positive and value == 0:
        raise LedgerError('positive number required')
    return value


class SingleInstance:
    """Local POSIX flock. Keep the lock inode; unlinking it defeats exclusion."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        if self.fd is not None:
            raise ResourceBusy('lock already held by this instance')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise ResourceBusy('dispatcher already running') from exc
        self.fd = fd
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


# These are bookkeeping transitions, not executable recovery instructions. In
# particular, none issues a fresh science launch; only claim/retry can reserve it.
# Entries: scenario, event -> allowed states, new state, independent status updates.
SCENARIOS = {
    ('S01', 'duplicate'): (None, None, {}),
    ('S02', 'existing_run'): (None, None, {}),
    ('S03', 'dependency_missing'): ({'CLAIMED'}, 'BLOCKED_DEPENDENCY', {}),
    ('S03', 'environment_missing'): ({'CLAIMED'}, 'BLOCKED_ENV', {}),
    ('S03', 'resource_missing'): ({'CLAIMED'}, 'BLOCKED_RESOURCE', {}),
    ('S04', 'change'): ({'CLAIMED'}, 'REPLAN', {}),
    ('S04', 'defer'): ({'CLAIMED'}, 'DEFERRED', {}),
    ('S04', 'expire'): ({'CLAIMED'}, 'EXPIRED', {}),
    ('S04', 'revoke'): (None, 'CANCELLING', {}),
    ('S05', 'candidate_valid'): ({'CLAIMED'}, 'SOURCE_VALIDATION', {}),
    ('S05', 'scope_changed'): ({'CLAIMED', 'SOURCE_VALIDATION'}, 'REPLAN', {}),
    ('S06', 'budget_exhausted'): ({'RETRY_WAIT', 'FAILED'}, 'BLOCKED_BUDGET', {}),
    ('S07', 'pause'): ({'CLAIMED', 'RETRY_WAIT'}, 'PAUSED', {}),
    ('S07', 'drain'): ({'RUNNING'}, 'DRAINING', {}),
    ('S07', 'cancel'): (None, 'CANCELLING', {}),
    ('S08', 'ownership_unknown'): (None, 'BLOCKED_RECONCILING', {}),
    ('S09', 'artifact_invalid'): ({'VALIDATING'}, 'CORRECTION', {}),
    ('S09', 'revalidate'): ({'CORRECTION'}, 'VALIDATING', {}),
    ('S09', 'validation_failed'): ({'VALIDATING', 'CORRECTION'}, 'FAILED', {'science_status': 'FAILED'}),
    ('S10', 'valid_result'): ({'VALIDATING'}, 'MERGE_GATE', {'science_status': 'COMPLETE', 'merge_status': 'PENDING'}),
    ('S11', 'head_changed'): ({'MERGE_GATE', 'MERGE_GATE_PENDING'}, 'MERGE_GATE_PENDING', {'merge_status': 'PENDING'}),
    ('S11', 'checks_pending'): ({'MERGE_GATE', 'MERGE_GATE_PENDING'}, 'MERGE_GATE_PENDING', {'merge_status': 'PENDING'}),
    ('S12', 'gate_failed'): ({'MERGE_GATE', 'MERGE_GATE_PENDING'}, 'EXCEPTION_REVIEW', {'merge_status': 'BLOCKED'}),
    ('S13', 'external_close'): (None, 'EXTERNAL_RECONCILING', {}),
    ('S13', 'external_merge'): (None, 'EXTERNAL_RECONCILING', {'merge_status': 'EXTERNAL_UNVERIFIED'}),
    ('S14', 'merge_unknown'): ({'MERGE_GATE', 'MERGE_GATE_PENDING'}, 'MERGE_UNKNOWN', {'merge_status': 'UNKNOWN'}),
    ('S14', 'merge_absent'): ({'MERGE_UNKNOWN'}, 'MERGE_GATE_PENDING', {'merge_status': 'PENDING'}),
    ('S14', 'merge_verified'): ({'MERGE_UNKNOWN', 'MERGE_GATE', 'MERGE_GATE_PENDING'}, 'CLOSE_PENDING', {'merge_status': 'MERGED', 'close_status': 'PENDING'}),
    ('S15', 'publish_failed'): ({'MERGE_GATE', 'MERGE_GATE_PENDING', 'PUBLISH_PENDING'}, 'PUBLISH_PENDING', {'publication_status': 'PENDING'}),
    ('S15', 'publish_verified'): ({'PUBLISH_PENDING', 'MERGE_GATE'}, 'MERGE_GATE', {'publication_status': 'PUBLISHED'}),
    ('S15', 'close_failed'): ({'CLOSE_PENDING'}, 'CLOSE_PENDING', {'close_status': 'PENDING'}),
    ('S15', 'close_verified'): ({'CLOSE_PENDING'}, 'REPORT_PENDING', {'close_status': 'CLOSED', 'report_status': 'PENDING'}),
    ('S15', 'report_failed'): ({'REPORT_PENDING'}, 'REPORT_PENDING', {'report_status': 'PENDING'}),
    ('S15', 'report_verified'): ({'REPORT_PENDING'}, 'COMPLETE', {'report_status': 'DELIVERED'}),
    ('S16', 'followup_requested'): ({'COMPLETE', 'REPORT_PENDING', 'CLOSE_PENDING'}, 'FOLLOWUP_REQUEST', {}),
    ('S17', 'input_missing'): ({'CLAIMED'}, 'FETCH_PENDING', {'storage_status': 'FETCH_PENDING'}),
    ('S17', 'input_verified'): ({'FETCH_PENDING'}, 'CLAIMED', {'storage_status': 'INPUT_VERIFIED'}),
    ('S18', 'archive_needed'): (None, 'ARCHIVE_PLAN', {'storage_status': 'ARCHIVE_PLAN'}),
    ('S18', 'archive_approval_needed'): ({'ARCHIVE_PLAN'}, 'ARCHIVE_APPROVAL_WAIT', {'storage_status': 'ARCHIVE_APPROVAL_WAIT'}),
    ('S18', 'archive_approved'): ({'ARCHIVE_APPROVAL_WAIT'}, 'COPYING', {'storage_status': 'COPYING'}),
    ('S18', 'archive_verified'): ({'COPYING'}, 'ARCHIVE_VERIFIED', {'storage_status': 'ARCHIVE_VERIFIED'}),
    ('S19', 'cleanup_needed'): ({'ARCHIVE_VERIFIED'}, 'CLEANUP_APPROVAL_WAIT', {'storage_status': 'CLEANUP_APPROVAL_WAIT'}),
    ('S19', 'cleanup_approved'): ({'CLEANUP_APPROVAL_WAIT'}, 'CLEANING', {'storage_status': 'CLEANING'}),
    ('S19', 'space_verified'): ({'CLEANING'}, 'SPACE_VERIFIED', {'storage_status': 'SPACE_VERIFIED'}),
    ('S20', 'storage_interrupted'): ({'FETCH_PENDING', 'COPYING', 'CLEANING'}, 'STORAGE_RECONCILING', {'storage_status': 'RECONCILING'}),
    ('S20', 'resume_fetch'): ({'STORAGE_RECONCILING'}, 'FETCH_PENDING', {'storage_status': 'FETCH_PENDING'}),
    ('S20', 'resume_copy'): ({'STORAGE_RECONCILING'}, 'COPYING', {'storage_status': 'COPYING'}),
    ('S20', 'resume_cleanup'): ({'STORAGE_RECONCILING'}, 'CLEANING', {'storage_status': 'CLEANING'}),
    ('S20', 'storage_blocked'): ({'STORAGE_RECONCILING'}, 'BLOCKED_STORAGE', {'storage_status': 'BLOCKED'}),
}


class Ledger:
    def __init__(self, path, *, fake_issuer_id='fake-issuer'):
        if fake_issuer_id != 'fake-issuer':
            raise LedgerError('only fake-issuer is supported; no live trust boundary')
        self.path = str(path)
        self.fake_issuer_id = fake_issuer_id
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES ('fence', 0);
                CREATE TABLE IF NOT EXISTS plans (
                  request_id TEXT NOT NULL, revision INTEGER NOT NULL,
                  digest TEXT NOT NULL, body TEXT NOT NULL,
                  PRIMARY KEY(request_id, revision));
                CREATE TABLE IF NOT EXISTS events (
                  event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_event_update BEFORE UPDATE ON events
                  BEGIN SELECT RAISE(ABORT, 'immutable event'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_event_delete BEFORE DELETE ON events
                  BEGIN SELECT RAISE(ABORT, 'immutable event'); END;
                CREATE TABLE IF NOT EXISTS approvals (
                  nonce TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                  request_id TEXT NOT NULL, revision INTEGER NOT NULL,
                  issued_at REAL NOT NULL, expires_at REAL NOT NULL,
                  body TEXT NOT NULL, consumed_by TEXT);
                CREATE TABLE IF NOT EXISTS revocations (
                  event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                  revision INTEGER NOT NULL, nonce TEXT, issued_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                  revision INTEGER NOT NULL, nonce TEXT UNIQUE NOT NULL,
                  plan TEXT NOT NULL, digest TEXT NOT NULL,
                  state TEXT NOT NULL, science_status TEXT NOT NULL,
                  publication_status TEXT NOT NULL, merge_status TEXT NOT NULL,
                  close_status TEXT NOT NULL, report_status TEXT NOT NULL,
                  storage_status TEXT NOT NULL, owner TEXT,
                  fence INTEGER NOT NULL, lease_expiry REAL NOT NULL,
                  observed_at REAL NOT NULL, attempt_count INTEGER NOT NULL,
                  spent_seconds REAL NOT NULL DEFAULT 0,
                  reserved_seconds REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts (
                  attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                  ordinal INTEGER NOT NULL, state TEXT NOT NULL, outcome TEXT,
                  reserved_seconds REAL NOT NULL, spent_seconds REAL NOT NULL DEFAULT 0,
                  process TEXT, UNIQUE(run_id, ordinal));
                CREATE TABLE IF NOT EXISTS machine (
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1), run_id TEXT UNIQUE);
                INSERT OR IGNORE INTO machine VALUES (1, NULL);
                CREATE TABLE IF NOT EXISTS transitions (
                  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                  scenario TEXT NOT NULL, event TEXT NOT NULL,
                  previous_state TEXT NOT NULL, next_state TEXT NOT NULL, at REAL NOT NULL);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _fence(db):
        db.execute("UPDATE metadata SET value=value+1 WHERE name='fence'")
        return db.execute("SELECT value FROM metadata WHERE name='fence'").fetchone()[0]

    def register_plan(self, plan, *, digest):
        body = plan.to_dict() if isinstance(plan, Plan) else plan
        plan = Plan.parse(body)
        if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise LedgerError('invalid private-plan digest')
        encoded = canonical(plan.to_dict()).decode()
        with self._transaction() as db:
            previous = db.execute('SELECT * FROM plans WHERE request_id=? AND revision=?',
                                  (plan.request_id, plan.revision)).fetchone()
            if previous:
                if previous['body'] != encoded or previous['digest'] != digest:
                    raise LedgerError('immutable plan revision changed')
                return False
            db.execute('INSERT INTO plans VALUES (?,?,?,?)',
                       (plan.request_id, plan.revision, digest, encoded))
        return True

    def _event(self, db, kind, record):
        encoded = canonical(record).decode()
        previous = db.execute('SELECT * FROM events WHERE event_id=?', (record['event_id'],)).fetchone()
        if previous:
            if previous['kind'] != kind or previous['body'] != encoded:
                raise LedgerError('immutable event changed')
            return False
        db.execute('INSERT INTO events VALUES (?,?,?)', (record['event_id'], kind, encoded))
        return True

    def _validate_event(self, record, required, optional=()):
        if type(record) is not dict or set(record) - (set(required) | set(optional) | {'version'}):
            raise LedgerError('unknown event fields')
        if set(required) - set(record):
            raise LedgerError('missing event fields')
        if type(record.get('version', 1)) is not int or record.get('version', 1) != 1:
            raise LedgerError('unsupported event version')
        if record['issuer_id'] != self.fake_issuer_id:
            raise LedgerError('unsupported fixture issuer')
        for key in ('event_id', 'request_id'):
            identifier(record[key])
        if type(record['revision']) is not int or record['revision'] < 1:
            raise LedgerError('invalid revision')
        _number(record['issued_at'])

    def ingest_approval(self, record):
        self._validate_event(record, ('event_id', 'issuer_id', 'request_id', 'plan_id', 'revision',
                                      'repo', 'issue', 'digest', 'nonce', 'scope', 'issued_at', 'expires_at'))
        if record['scope'] != ['cpu_fixture']:
            raise LedgerError('only CPU fixture scope is supported')
        identifier(record['nonce'])
        identifier(record['plan_id'])
        _number(record['expires_at'])
        if record['expires_at'] <= record['issued_at']:
            raise LedgerError('invalid approval validity interval')
        with self._transaction() as db:
            if not self._event(db, 'approval', record):
                return False
            existing = db.execute('SELECT nonce FROM approvals WHERE nonce=?', (record['nonce'],)).fetchone()
            if existing:
                raise LedgerError('nonce replay or conflicting approval')
            db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,NULL)',
                       (record['nonce'], record['event_id'], record['request_id'], record['revision'],
                        record['issued_at'], record['expires_at'], canonical(record).decode()))
        return True

    def ingest_revocation(self, record):
        self._validate_event(record, ('event_id', 'issuer_id', 'request_id', 'revision', 'issued_at'), ('nonce',))
        if 'nonce' in record:
            identifier(record['nonce'])
        with self._transaction() as db:
            if not self._event(db, 'revocation', record):
                return False
            db.execute('INSERT INTO revocations VALUES (?,?,?,?,?)',
                       (record['event_id'], record['request_id'], record['revision'],
                        record.get('nonce'), record['issued_at']))
            # Event order is irrelevant. Retain the revocation even before the
            # approval or plan arrives. Never release an unconfirmed process.
            db.execute('''UPDATE runs SET state='CANCELLING'
                          WHERE request_id=? AND revision=? AND (? IS NULL OR nonce=?)
                          AND science_status IN ('PENDING', 'RUNNING')''',
                       (record['request_id'], record['revision'], record.get('nonce'), record.get('nonce')))
        return True

    @staticmethod
    def _revoked(db, request_id, revision, nonce):
        return db.execute('''SELECT 1 FROM revocations WHERE request_id=? AND revision=?
                             AND (nonce IS NULL OR nonce=?)''', (request_id, revision, nonce)).fetchone() is not None

    @staticmethod
    def _reserve_machine(db, run_id):
        if db.execute('SELECT run_id FROM machine WHERE singleton=1').fetchone()[0] is not None:
            raise ResourceBusy('single machine already reserved')
        db.execute('UPDATE machine SET run_id=? WHERE singleton=1', (run_id,))

    def claim(self, request_id, *, owner, now, lease_seconds=180):
        identifier(owner)
        _number(now)
        _number(lease_seconds, positive=True)
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM runs WHERE request_id=?', (request_id,)).fetchone():
                raise AlreadyClaimed('request already has a run; inspect existing state')
            row = db.execute('SELECT * FROM plans WHERE request_id=? ORDER BY revision DESC LIMIT 1',
                             (request_id,)).fetchone()
            if row is None:
                raise LedgerError('unknown request')
            plan = Plan.parse(json.loads(row['body']))
            approvals = db.execute('''SELECT * FROM approvals WHERE request_id=? AND revision=?
                AND issued_at<=? AND expires_at>? ORDER BY issued_at DESC, event_id''',
                                   (request_id, plan.revision, now, now)).fetchall()
            chosen = None
            for approval in approvals:
                record = json.loads(approval['body'])
                if approval['consumed_by'] or self._revoked(db, request_id, plan.revision, approval['nonce']):
                    continue
                expected = {'plan_id': plan.plan_id, 'repo': plan.repo, 'issue': plan.issue, 'digest': row['digest']}
                if all(type(record[k]) is type(v) and record[k] == v for k, v in expected.items()):
                    chosen = approval
                    break
            if chosen is None:
                raise LedgerError('no current matching unrevoked approval')
            run_id, attempt_id = 'run-' + uuid.uuid4().hex, 'attempt-' + uuid.uuid4().hex
            # Deliberately consume before the resource write; failed reservation
            # must roll back consumption, fence, run, and budget together.
            db.execute('UPDATE approvals SET consumed_by=? WHERE nonce=?', (run_id, chosen['nonce']))
            self._reserve_machine(db, run_id)
            fence = self._fence(db)
            db.execute('''INSERT INTO runs VALUES
                (?,?,?,?,?,?,'CLAIMED','PENDING','NOT_STARTED','NOT_STARTED','NOT_STARTED',
                 'NOT_STARTED',?,?,?,?,?,1,0,?)''',
                       (run_id, request_id, plan.revision, chosen['nonce'], row['body'], row['digest'],
                        'PENDING' if plan.storage_required else 'NOT_REQUIRED', owner, fence,
                        now + lease_seconds, now, plan.budget.wall_seconds))
            db.execute("INSERT INTO attempts VALUES (?,?,1,'RESERVED',NULL,?,0,NULL)",
                       (attempt_id, run_id, plan.budget.wall_seconds))
            return Claim(run_id, attempt_id, fence)

    @staticmethod
    def _get(db, run_id):
        row = db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise LedgerError('unknown run')
        return row

    def _owned(self, db, run_id, fence, now, *, lease=True):
        _number(now)
        row = self._get(db, run_id)
        if type(fence) is not int or fence != row['fence'] or row['owner'] is None:
            raise StaleFence('stale or unowned fencing token')
        if now < row['observed_at']:
            raise LedgerError('clock moved backwards; reconcile required')
        if lease and now >= row['lease_expiry']:
            raise StaleFence('lease expired; process reconciliation required')
        db.execute('UPDATE runs SET observed_at=? WHERE run_id=?', (now, run_id))
        return row

    def mark_running(self, run_id, fence, *, process, now):
        if type(process) is not dict or set(process) != {'pid', 'start_time', 'boot_id', 'process_group'}:
            raise LedgerError('complete process identity required')
        if any(type(process[k]) is not int or process[k] <= 0 for k in ('pid', 'process_group')):
            raise LedgerError('invalid process identity')
        if (type(process['boot_id']) is not str or not process['boot_id']
                or type(process['start_time']) is not int or process['start_time'] <= 0):
            raise LedgerError('invalid process identity')
        with self._transaction() as db:
            row = self._owned(db, run_id, fence, now)
            if row['state'] not in ('CLAIMED', 'SOURCE_VALIDATION'):
                raise InvalidTransition('run cannot enter RUNNING')
            plan = Plan.parse(json.loads(row['plan']))
            if plan.storage_required and row['storage_status'] != 'INPUT_VERIFIED':
                raise LedgerError('required storage input has not been verified')
            if self._revoked(db, row['request_id'], row['revision'], row['nonce']):
                raise LedgerError('approval revoked before launch')
            latest = db.execute('SELECT MAX(revision) FROM plans WHERE request_id=?', (row['request_id'],)).fetchone()[0]
            expiry = db.execute('SELECT expires_at FROM approvals WHERE nonce=?', (row['nonce'],)).fetchone()[0]
            if latest != row['revision'] or now >= expiry:
                raise LedgerError('approval revision or expiry changed before launch')
            db.execute("UPDATE runs SET state='RUNNING', science_status='RUNNING' WHERE run_id=?", (run_id,))
            db.execute("UPDATE attempts SET state='RUNNING', process=? WHERE run_id=? AND ordinal=?",
                       (canonical(process).decode(), run_id, row['attempt_count']))

    def heartbeat(self, run_id, fence, *, now, lease_seconds=180):
        _number(lease_seconds, positive=True)
        with self._transaction() as db:
            self._owned(db, run_id, fence, now)
            db.execute('UPDATE runs SET lease_expiry=? WHERE run_id=?', (now + lease_seconds, run_id))

    def _finish(self, db, row, *, elapsed_seconds, outcome):
        plan = Plan.parse(json.loads(row['plan']))
        spent = row['spent_seconds'] + elapsed_seconds
        cancel_seen = db.execute("""SELECT 1 FROM transitions WHERE run_id=?
            AND ((scenario='S07' AND event='cancel') OR (scenario='S04' AND event='revoke'))""",
                                 (row['run_id'],)).fetchone() is not None
        external_seen = db.execute("SELECT 1 FROM transitions WHERE run_id=? AND scenario='S13'",
                                   (row['run_id'],)).fetchone() is not None
        if spent > plan.budget.total_seconds or elapsed_seconds > row['reserved_seconds']:
            state, science = 'BLOCKED_BUDGET', 'FAILED'
        elif (outcome == 'cancelled' or row['state'] == 'CANCELLING' or cancel_seen
              or self._revoked(db, row['request_id'], row['revision'], row['nonce'])):
            state, science = 'CANCELLED', 'CANCELLED'
        elif row['state'] == 'EXTERNAL_RECONCILING' or external_seen:
            state = 'EXTERNAL_RECONCILING'
            science = 'AWAITING_VALIDATION' if outcome == 'success' else 'FAILED'
        elif outcome == 'success':
            state, science = 'VALIDATING', 'AWAITING_VALIDATION'
        elif (outcome in plan.retry_codes and row['attempt_count'] < plan.budget.max_attempts
              and spent + plan.budget.wall_seconds <= plan.budget.total_seconds):
            state, science = 'RETRY_WAIT', 'FAILED'
        else:
            state, science = 'FAILED', 'FAILED'
        db.execute('''UPDATE attempts SET state='FINISHED', outcome=?, spent_seconds=?, reserved_seconds=0
                      WHERE run_id=? AND ordinal=?''', (outcome, elapsed_seconds, row['run_id'], row['attempt_count']))
        db.execute('''UPDATE runs SET state=?, science_status=?, spent_seconds=?, reserved_seconds=0
                      WHERE run_id=?''', (state, science, spent, row['run_id']))
        db.execute('UPDATE machine SET run_id=NULL WHERE singleton=1 AND run_id=?', (row['run_id'],))

    @staticmethod
    def _outcome(outcome, elapsed_seconds):
        _number(elapsed_seconds)
        if outcome not in ('success', 'transient_io', 'transient_api', 'oom', 'failed', 'cancelled', 'timeout'):
            raise LedgerError('unknown outcome; performance-based retries are prohibited')

    def finish_attempt(self, run_id, fence, *, now, elapsed_seconds, outcome, process_gone):
        self._outcome(outcome, elapsed_seconds)
        if process_gone is not True:
            raise LedgerError('process termination must be confirmed before release')
        with self._transaction() as db:
            row = self._owned(db, run_id, fence, now)
            attempt = db.execute('SELECT state FROM attempts WHERE run_id=? AND ordinal=?',
                                 (run_id, row['attempt_count'])).fetchone()
            if attempt['state'] not in ('RESERVED', 'RUNNING'):
                raise InvalidTransition('attempt already finished')
            self._finish(db, row, elapsed_seconds=elapsed_seconds, outcome=outcome)

    def retry(self, run_id, *, owner, now, lease_seconds=180):
        identifier(owner)
        _number(now)
        _number(lease_seconds, positive=True)
        with self._transaction() as db:
            row = self._get(db, run_id)
            if now < row['observed_at']:
                raise LedgerError('clock moved backwards')
            if row['state'] != 'RETRY_WAIT':
                raise InvalidTransition('only explicitly retryable failures may retry')
            plan = Plan.parse(json.loads(row['plan']))
            if self._revoked(db, row['request_id'], row['revision'], row['nonce']):
                raise LedgerError('approval revoked')
            latest = db.execute('SELECT MAX(revision) FROM plans WHERE request_id=?', (row['request_id'],)).fetchone()[0]
            approval = db.execute('SELECT expires_at FROM approvals WHERE nonce=?', (row['nonce'],)).fetchone()
            if latest != row['revision'] or now >= approval['expires_at']:
                raise LedgerError('approval stale or expired')
            if row['attempt_count'] >= plan.budget.max_attempts or row['spent_seconds'] + plan.budget.wall_seconds > plan.budget.total_seconds:
                raise LedgerError('cumulative budget exhausted')
            self._reserve_machine(db, run_id)
            fence, attempt_id = self._fence(db), 'attempt-' + uuid.uuid4().hex
            ordinal = row['attempt_count'] + 1
            db.execute('''UPDATE runs SET owner=?, fence=?, lease_expiry=?, observed_at=?,
                          state='CLAIMED', science_status='PENDING', attempt_count=?, reserved_seconds=? WHERE run_id=?''',
                       (owner, fence, now + lease_seconds, now, ordinal, plan.budget.wall_seconds, run_id))
            db.execute("INSERT INTO attempts VALUES (?,?,?,'RESERVED',NULL,?,0,NULL)",
                       (attempt_id, run_id, ordinal, plan.budget.wall_seconds))
            return Claim(run_id, attempt_id, fence)

    def expire_leases(self, *, now):
        _number(now)
        expired = []
        with self._transaction() as db:
            rows = db.execute('''SELECT r.* FROM runs r JOIN machine m ON m.run_id=r.run_id
                                 WHERE r.lease_expiry<=? AND r.state!='BLOCKED_RECONCILING' ''', (now,)).fetchall()
            for row in rows:
                fence = self._fence(db)
                db.execute("UPDATE runs SET state='BLOCKED_RECONCILING', owner=NULL, fence=?, observed_at=? WHERE run_id=?",
                           (fence, now, row['run_id']))
                expired.append(row['run_id'])
        return expired

    def reconcile(self, run_id, *, owner, now, process_gone, elapsed_seconds, outcome, lease_seconds=180):
        """Accept explicit fixture readback, never infer death from an expired lease.

        Production must replace the boolean readback with protected process-manager
        evidence. Unknown/alive identities remain blocked; this method never kills.
        """
        self._outcome(outcome, elapsed_seconds)
        identifier(owner)
        _number(now)
        _number(lease_seconds, positive=True)
        if process_gone is not True:
            raise LedgerError('ownership or process termination still unknown')
        with self._transaction() as db:
            row = self._get(db, run_id)
            if row['state'] != 'BLOCKED_RECONCILING' or now < row['observed_at']:
                raise InvalidTransition('run is not awaiting process reconciliation')
            fence = self._fence(db)
            db.execute('UPDATE runs SET owner=?, fence=?, observed_at=?, lease_expiry=? WHERE run_id=?',
                       (owner, fence, now, now + lease_seconds, run_id))
            self._finish(db, row, elapsed_seconds=elapsed_seconds, outcome=outcome)
            attempt_id = db.execute('SELECT attempt_id FROM attempts WHERE run_id=? AND ordinal=?',
                                    (run_id, row['attempt_count'])).fetchone()[0]
            return Claim(run_id, attempt_id, fence)

    def resume_aftermath(self, run_id, *, owner, now, lease_seconds=180):
        """Fence a restarted publisher without granting new science-launch rights."""
        identifier(owner)
        _number(now)
        _number(lease_seconds, positive=True)
        with self._transaction() as db:
            row = self._get(db, run_id)
            if now < row['observed_at']:
                raise LedgerError('clock moved backwards')
            if row['owner'] is not None and now < row['lease_expiry']:
                raise ResourceBusy('aftermath owner still holds a valid lease')
            active = db.execute("SELECT 1 FROM attempts WHERE run_id=? AND state!='FINISHED'", (run_id,)).fetchone()
            if (active or row['reserved_seconds'] or row['science_status'] not in ('AWAITING_VALIDATION', 'COMPLETE')
                    or db.execute('SELECT 1 FROM machine WHERE run_id=?', (run_id,)).fetchone()):
                raise InvalidTransition('only completed science may resume aftermath')
            fence = self._fence(db)
            db.execute('UPDATE runs SET owner=?, fence=?, observed_at=?, lease_expiry=? WHERE run_id=?',
                       (owner, fence, now, now + lease_seconds, run_id))
            attempt_id = db.execute('SELECT attempt_id FROM attempts WHERE run_id=? AND ordinal=?',
                                    (run_id, row['attempt_count'])).fetchone()[0]
            return Claim(run_id, attempt_id, fence)

    def transition(self, run_id, fence, scenario, event, *, now):
        spec = SCENARIOS.get((scenario, event))
        if spec is None:
            raise InvalidTransition('unknown scenario or event')
        allowed, target, changes = spec
        with self._transaction() as db:
            row = self._owned(db, run_id, fence, now)
            if allowed is not None and row['state'] not in allowed:
                raise InvalidTransition('event is not allowed from current state')
            active_aftermath = (scenario in ('S10', 'S11', 'S14')
                                or (scenario == 'S15' and event.startswith('publish_')))
            if active_aftermath and self._revoked(db, row['request_id'], row['revision'], row['nonce']):
                raise InvalidTransition('approval revoked; publication and merge advancement blocked')
            if scenario in ('S10', 'S11', 'S12', 'S14', 'S15', 'S16', 'S18', 'S19'):
                if row['reserved_seconds'] or row['science_status'] not in ('AWAITING_VALIDATION', 'COMPLETE'):
                    raise InvalidTransition('scientific result required for aftermath')
            if scenario == 'S20' and event.startswith('resume_'):
                prior = db.execute('''SELECT previous_state FROM transitions WHERE run_id=?
                    AND scenario='S20' AND event='storage_interrupted' ORDER BY id DESC LIMIT 1''', (run_id,)).fetchone()
                if prior is None or prior[0] != target:
                    raise InvalidTransition('storage may only resume interrupted stage')
            updates = dict(changes, state=target or row['state'])
            if (scenario, event) == ('S08', 'ownership_unknown'):
                updates.update(owner=None, fence=self._fence(db))
            names = ','.join(key + '=?' for key in updates)
            db.execute(f'UPDATE runs SET {names} WHERE run_id=?', (*updates.values(), run_id))
            db.execute('INSERT INTO transitions(run_id,scenario,event,previous_state,next_state,at) VALUES (?,?,?,?,?,?)',
                       (run_id, scenario, event, row['state'], target or row['state'], now))
        return self.get_run(run_id)

    def get_run(self, run_id):
        with self._connect() as db:
            result = dict(self._get(db, run_id))
            result['plan'] = json.loads(result['plan'])
            result['attempts'] = [dict(row) for row in db.execute('SELECT * FROM attempts WHERE run_id=? ORDER BY ordinal', (run_id,))]
            for attempt in result['attempts']:
                attempt['process'] = json.loads(attempt['process']) if attempt['process'] else None
            return result

    def counts(self):
        with self._connect() as db:
            return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in ('plans', 'events', 'approvals', 'revocations', 'runs', 'attempts')}

    def machine_owner(self):
        with self._connect() as db:
            return db.execute('SELECT run_id FROM machine WHERE singleton=1').fetchone()[0]
