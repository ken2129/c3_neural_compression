"""Manager-owned durable offline fixture. No live runtime or automatic reset."""
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from .intake_flow_fixture import FIXTURE_NUMBER, FixtureSource, run_fixture
from .issue_intake import Intake, content_hash
from .owner_approval import inspect_once
from .intake_flow_fixture import FIXTURE_NOW
from .schema import ContractError, canonical, loads

VERSION = 1


def private(path, directory=False):
    info = path.lstat()
    expected = 0o700 if directory else 0o600
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or stat.S_IMODE(info.st_mode) != expected or info.st_uid != os.getuid()
            or (not directory and info.st_nlink != 1)):
        raise ContractError('state ownership/type/permissions mismatch')


def sync_directory(root):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def exclusive_record(path, value):
    fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        sync_directory(path.parent)
    except BaseException:
        # Keep partial initialization as evidence; never silently reset it.
        raise


def initialize(root):
    root=Path(root); private(root, True)
    if set(p.name for p in root.iterdir()) not in ({'.layout'}, {'.layout', 'manager.lock'}) or (root/'.layout').read_text() != 'offline-fixture-v1\n':
        raise ContractError('initialization requires a fresh dedicated volume')
    if (root/'manager.lock').exists(): private(root/'manager.lock')
    nonce=uuid.uuid4().hex
    exclusive_record(root/'initialized.json',dict(version=VERSION,nonce=nonce))
    oldmask=os.umask(0o077)
    try:
        intake=Intake(root/'state.sqlite')
        try:
            source=FixtureSource()
            inspect_once(source,source,intake,FIXTURE_NUMBER,lambda:FIXTURE_NOW)
            intake.db.execute('CREATE TABLE offline_dummy_runs (issue INTEGER PRIMARY KEY, digest TEXT NOT NULL, phase TEXT NOT NULL, result TEXT)')
            intake.db.execute('CREATE TABLE volume_identity (nonce TEXT PRIMARY KEY NOT NULL)')
            intake.db.execute('INSERT INTO volume_identity VALUES (?)',(nonce,))
            intake.db.execute('PRAGMA user_version=1')
            intake.db.commit()
        finally: intake.close()
        sync_directory(root)
    finally: os.umask(oldmask)
    return dict(fixture_only=True,execution_allowed=False,initialized=True)


EXPECTED_SQL={
    'intake': 'CREATE TABLE intake (number INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, changed INTEGER NOT NULL, observations INTEGER NOT NULL, observed_status TEXT NOT NULL)',
    'owner_receipts': 'CREATE TABLE owner_receipts (issue INTEGER PRIMARY KEY, comment TEXT NOT NULL UNIQUE, receipt_hash TEXT NOT NULL, content_hash TEXT NOT NULL)',
    'owner_approval_blocks': 'CREATE TABLE owner_approval_blocks (issue INTEGER PRIMARY KEY)',
    'offline_dummy_runs': 'CREATE TABLE offline_dummy_runs (issue INTEGER PRIMARY KEY, digest TEXT NOT NULL, phase TEXT NOT NULL, result TEXT)',
    'volume_identity': 'CREATE TABLE volume_identity (nonce TEXT PRIMARY KEY NOT NULL)',
}


def open_existing(root):
    root=Path(root); private(root,True)
    for name in ('initialized.json','state.sqlite'):
        private(root/name)
    marker=loads((root/'initialized.json').read_bytes())
    if type(marker) is not dict or set(marker) != {'version','nonce'} or marker['version'] != VERSION:
        raise ContractError('unknown volume initialization')
    # Never let SQLite create a missing database on a normal run.
    db=sqlite3.connect((root/'state.sqlite').as_uri()+'?mode=rw',uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA user_version').fetchone() != (VERSION,):
            raise ContractError('database corruption or unknown version')
        objects=db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if set(objects) != {('table',name) for name in EXPECTED_SQL}:
            raise ContractError('unknown or missing schema objects')
        actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        if actual != EXPECTED_SQL:
            raise ContractError('schema mismatch')
        if db.execute('SELECT nonce FROM volume_identity').fetchall() != [(marker['nonce'],)]:
            raise ContractError('volume/database identity mismatch')
        source=FixtureSource()
        expected_receipt=(FIXTURE_NUMBER,source.comment['id'],hashlib.sha256(canonical(source.comment)).hexdigest(),content_hash(source.issue))
        if db.execute('SELECT * FROM owner_receipts').fetchall() != [expected_receipt]:
            raise ContractError('saved approval state missing or inconsistent')
        intake_rows=db.execute('SELECT number,fingerprint FROM intake').fetchall()
        if intake_rows != [(FIXTURE_NUMBER,content_hash(source.issue))]:
            raise ContractError('saved intake state missing or inconsistent')
        rows=db.execute('SELECT issue,digest,phase,result FROM offline_dummy_runs').fetchall()
        if len(rows)>1 or (rows and rows[0][0] != FIXTURE_NUMBER):
            raise ContractError('unexpected run identity')
        witness=root/'dispatch.json'
        if witness.exists() or witness.is_symlink():
            private(witness)
            record=loads(witness.read_bytes())
            if not rows or record != {'fixture_only':True,'issue':FIXTURE_NUMBER,'content_sha256':rows[0][1]}:
                raise ContractError('dispatch witness does not match saved claim')
        elif rows and rows[0][2]=='COMPLETED':
            raise ContractError('completed run missing dispatch witness')
    except BaseException:
        db.close()
        raise
    # Keep the validated mode=rw connection; never reopen with create semantics.
    intake=Intake.__new__(Intake)
    intake.db=db
    return intake


def run_persistent(root, exchange):
    root=Path(root)
    intake=open_existing(root)
    try:
        def once(job):
            exclusive_record(root/'dispatch.json',job)
            return exchange(job)
        result=run_fixture(intake,FixtureSource(),once)
        if result['phase'] != 'COMPLETED':
            raise ContractError('unfinished prior claim requires reconciliation')
        return dict(result, result_saved=True)
    finally: intake.close()
