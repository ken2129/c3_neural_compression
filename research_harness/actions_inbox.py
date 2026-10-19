"""Issue92 durable notification delivery, never execution authorization.

The runner's job ends after this inbox commits. Only the manager subsequently
loads the current Issue and invokes the existing formal approval/claim path.
No Issue listing, ref fetching, shell, or model launch belongs in this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time

from .schema import ContractError, canonical, fields, loads

REPOSITORY = 'ken2129/c3_neural_compression'
REPOSITORY_ID = 1312915009
OWNER_ID = 55587773
MANAGER_UID = 10001
RUNNER_UID = 10003
SOCKET_PATH = '/control-ipc/events.sock'
MAX_RECEIPT = 8192
MAX_ACK = 4096
ACTIONS = {'issues': frozenset(('opened', 'edited', 'reopened', 'closed', 'labeled', 'unlabeled')),
           'issue_comment': frozenset(('created', 'edited', 'deleted'))}
RECEIPT_FIELDS = ('schema', 'repository', 'repository_id', 'issue_number', 'event_name',
                  'action', 'actor_id', 'comment_id', 'run_id', 'run_attempt',
                  'workflow_sha', 'receipt_id')


class IntakeBlocked(ContractError):
    """Fixed local categories only; never include an external value."""
    CODES = frozenset(('INTAKE_DISABLED', 'RECEIPT_REJECTED', 'WORKFLOW_REJECTED',
                       'RECEIPT_CONFLICT', 'INBOX_FULL', 'IPC_REJECTED', 'IPC_FAILED',
                       'INBOX_FAILED', 'CLEANUP_UNVERIFIED'))

    def __init__(self, code):
        self.code = code if code in self.CODES else 'INBOX_FAILED'
        super().__init__(self.code)


def validate_policy(value):
    fields(value, ('schema', 'enabled', 'workflow_shas'))
    if (value['schema'] != 'c3-resident-intake-v1' or type(value['enabled']) is not bool
            or type(value['workflow_shas']) is not list or not 1 <= len(value['workflow_shas']) <= 8
            or any(type(s) is not str or re.fullmatch('[0-9a-f]{40}', s) is None
                   for s in value['workflow_shas'])
            or len(set(value['workflow_shas'])) != len(value['workflow_shas'])):
        raise IntakeBlocked('WORKFLOW_REJECTED')
    return value


def validate_receipt(value, policy):
    validate_policy(policy)
    if not policy['enabled']:
        raise IntakeBlocked('INTAKE_DISABLED')
    fields(value, RECEIPT_FIELDS)
    if (value['schema'] != 'c3-issue-event-v1' or value['repository'] != REPOSITORY
            or type(value['repository_id']) is not int or value['repository_id'] != REPOSITORY_ID
            or type(value['actor_id']) is not int or value['actor_id'] != OWNER_ID):
        raise IntakeBlocked('RECEIPT_REJECTED')
    for key in ('issue_number', 'run_id', 'run_attempt'):
        if type(value[key]) is not int or not 0 < value[key] < 2**63:
            raise IntakeBlocked('RECEIPT_REJECTED')
    if (type(value['event_name']) is not str or type(value['action']) is not str
            or value['event_name'] not in ACTIONS or value['action'] not in ACTIONS[value['event_name']]):
        raise IntakeBlocked('RECEIPT_REJECTED')
    if value['event_name'] == 'issues':
        if value['comment_id'] is not None:
            raise IntakeBlocked('RECEIPT_REJECTED')
    elif type(value['comment_id']) is not int or not 0 < value['comment_id'] < 2**63:
        raise IntakeBlocked('RECEIPT_REJECTED')
    if (type(value['workflow_sha']) is not str or value['workflow_sha'] not in policy['workflow_shas']
            or value['receipt_id'] != f"actions:{REPOSITORY_ID}:{value['run_id']}"):
        raise IntakeBlocked('WORKFLOW_REJECTED')
    return value


def acknowledge(receipt, status):
    if status not in ('enqueued', 'duplicate'):
        raise IntakeBlocked('INBOX_FAILED')
    return dict(schema='c3-issue-event-ack-v1', receipt_id=receipt['receipt_id'], status=status)


class Inbox:
    """One private SQLite DB, independent of the model/claim connection.

    Each caller/thread opens its own connection. FULL synchronous commits
    precede ACK. Attempts are metadata, not delivery identity. A later owner
    approval has a new Actions run ID and gets its own queue row.
    """
    def __init__(self, path, *, maximum_pending=1024):
        self.path = Path(path)
        self.maximum_pending = maximum_pending
        self.wake = threading.Event()
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise IntakeBlocked('INBOX_FAILED')
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise IntakeBlocked('INBOX_FAILED')
        else:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS events ('
                'receipt TEXT PRIMARY KEY, identity TEXT NOT NULL, payload TEXT NOT NULL, '
                'last_attempt INTEGER NOT NULL, phase TEXT NOT NULL, outcome TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS gate (singleton INTEGER PRIMARY KEY CHECK(singleton=1), reason TEXT NOT NULL)')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def enqueue(self, receipt):
        identity = hashlib.sha256(canonical({k: v for k, v in receipt.items() if k != 'run_attempt'})).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT identity FROM events WHERE receipt=?', (receipt['receipt_id'],)).fetchone()
            if prior:
                if prior[0] != identity:
                    raise IntakeBlocked('RECEIPT_CONFLICT')
                db.execute('UPDATE events SET last_attempt=max(last_attempt,?) WHERE receipt=?',
                           (receipt['run_attempt'], receipt['receipt_id']))
                status = 'duplicate'
            else:
                if db.execute('SELECT 1 FROM gate').fetchone():
                    raise IntakeBlocked('CLEANUP_UNVERIFIED')
                if db.execute("SELECT count(*) FROM events WHERE phase IN ('PENDING','ACTIVE')").fetchone()[0] >= self.maximum_pending:
                    raise IntakeBlocked('INBOX_FULL')
                db.execute('INSERT INTO events VALUES (?,?,?,?,?,NULL)',
                           (receipt['receipt_id'], identity, canonical(receipt).decode(), receipt['run_attempt'], 'PENDING'))
                status = 'enqueued'
            db.commit()  # No model waiting, and absolutely no ACK before here.
        self.wake.set()
        return acknowledge(receipt, status)

    def recover(self):
        # Called only after boot has proved there are no prior worker processes
        # or runtime files. This changes delivery leases, never service claims.
        with self.connect() as db:
            if db.execute('SELECT 1 FROM gate').fetchone():
                raise IntakeBlocked('CLEANUP_UNVERIFIED')
            db.execute("UPDATE events SET phase='PENDING' WHERE phase='ACTIVE'")
        self.wake.set()

    def take(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM gate').fetchone():
                raise IntakeBlocked('CLEANUP_UNVERIFIED')
            row = db.execute("SELECT receipt,payload FROM events WHERE phase='PENDING' ORDER BY rowid LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE events SET phase='ACTIVE' WHERE receipt=?", (row[0],))
        return None if row is None else loads(row[1])

    def finish(self, receipt_id, report):
        # Only closed manager metadata, never summaries, stderr or model output.
        allowed = {'issue', 'phase', 'dispatched', 'duplicate', 'outcome', 'pr_url', 'reason'}
        safe = {k: v for k, v in report.items() if k in allowed}
        with self.connect() as db:
            cursor = db.execute("UPDATE events SET phase='FINISHED',outcome=? WHERE receipt=? AND phase='ACTIVE'",
                                (canonical(safe).decode(), receipt_id))
            if cursor.rowcount != 1:
                raise IntakeBlocked('INBOX_FAILED')

    def block(self):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO gate VALUES (1,'CLEANUP_UNVERIFIED')")


def _line(stream, maximum):
    raw = stream.readline(maximum + 1)
    if not raw or len(raw) > maximum or not raw.endswith(b'\n'):
        raise IntakeBlocked('RECEIPT_REJECTED')
    return loads(raw)


class ReceiptServer:
    """A manager-owned thread only; never invokes Controller or reads secrets."""
    def __init__(self, path, inbox, policy, *, runner_uid=RUNNER_UID):
        self.path, self.inbox, self.policy = Path(path), inbox, validate_policy(policy)
        self.runner_uid = runner_uid
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        info = self.path.parent.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o711:
            raise IntakeBlocked('IPC_REJECTED')
        if self.path.exists() or self.path.is_symlink():
            raise IntakeBlocked('IPC_REJECTED')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        # Access is authenticated by kernel peer UID, never receipt actor_id.
        # Only manager can create/unlink entries in the 0711 parent directory.
        os.chmod(self.path, 0o666)
        self.listener.listen(16)
        self.listener.settimeout(.25)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != self.runner_uid:
                        raise IntakeBlocked('IPC_REJECTED')
                    with connection.makefile('rb') as stream:
                        receipt = validate_receipt(_line(stream, MAX_RECEIPT), self.policy)
                        if stream.read(1):
                            raise IntakeBlocked('RECEIPT_REJECTED')
                        ack = self.inbox.enqueue(receipt)
                    connection.sendall(canonical(ack) + b'\n')
                except (Exception,):
                    # No arbitrary exception or remote payload is serialized.
                    try: connection.sendall(b'{"error":"RECEIPT_REJECTED"}\n')
                    except OSError: pass

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=3)
        self.path.unlink()


def submit_main(argv=None):
    """Installed CLI: exactly the dot-side --receipt-stdin contract."""
    previous = None
    try:
        # Bound stdin/EOF as well as connect/ACK, below the client's 30 seconds.
        # This fixed executable is a standalone single-threaded Linux process.
        def expired(*_): raise IntakeBlocked('IPC_FAILED')
        previous = signal.signal(signal.SIGALRM, expired)
        signal.alarm(25)
        if (sys.argv[1:] if argv is None else argv) != ['--receipt-stdin'] or os.getuid() != RUNNER_UID:
            raise IntakeBlocked('IPC_REJECTED')
        item = _line(sys.stdin.buffer, MAX_RECEIPT)
        if sys.stdin.buffer.read(1):
            raise IntakeBlocked('RECEIPT_REJECTED')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(20)
            connection.connect(SOCKET_PATH)
            _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != MANAGER_UID:
                raise IntakeBlocked('IPC_REJECTED')
            connection.sendall(canonical(item) + b'\n')
            connection.shutdown(socket.SHUT_WR)
            with connection.makefile('rb') as stream:
                ack = _line(stream, MAX_ACK)
            fields(ack, ('schema', 'receipt_id', 'status'))
            if ack != acknowledge(item, ack['status']):
                raise IntakeBlocked('IPC_FAILED')
        sys.stdout.buffer.write(canonical(ack) + b'\n')
        return 0
    except Exception:
        print('ISSUE_EVENT_DELIVERY_FAILED', file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
        if previous is not None: signal.signal(signal.SIGALRM, previous)
