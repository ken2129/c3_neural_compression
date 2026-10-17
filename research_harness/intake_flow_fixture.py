"""Offline diagnostic plumbing only: synthetic approval -> fixed dummy -> ledger.

No live client, arbitrary command, model, credentials or production enable flag.
Synthetic owner metadata here is NOT evidence of an actual owner's approval.
"""
import copy
import hashlib
import json

from .issue_intake import API, MARKER, PREFIX, Intake, content_hash, snapshot
from .owner_approval import OWNER_ID, OWNER_LOGIN, SCHEMA, inspect_once
from .schema import ContractError, REPOSITORY, canonical, fields

FIXTURE_NOW = 1800000000
FIXTURE_NUMBER = 999999999


class FixtureSource:
    def __init__(self):
        request = dict(schema_version=1, plan_version=1, issuer=OWNER_LOGIN,
                       approval_ref='offline-fixture-only', expires_at='2030-01-01T00:00:00Z',
                       cancelled=False, plan={'purpose': 'fixed dummy checksum, never research'})
        self.issue = snapshot(dict(number=FIXTURE_NUMBER, url=f'{API}/issues/{FIXTURE_NUMBER}',
                                   repository_url=API, title=PREFIX + ' OFFLINE SYNTHETIC FIXTURE',
                                   body='Not a real Issue.' + MARKER + json.dumps(request) + '\n```',
                                   user={'id': OWNER_ID}, state='open', updated_at='2026-10-17T00:00:00Z'))
        body = dict(schema=SCHEMA, request_id=request['approval_ref'], repository=REPOSITORY,
                    issue=FIXTURE_NUMBER, plan_version=1, content_sha256=content_hash(self.issue),
                    expires_at=request['expires_at'], decision='approve-execution')
        self.comment = dict(id='IC_OFFLINE_SYNTHETIC_ONLY', body=json.dumps(body),
                            author={'login': OWNER_LOGIN, 'databaseId': OWNER_ID},
                            createdAt='2026-10-17T00:00:00Z', updatedAt='2026-10-17T00:00:00Z',
                            lastEditedAt=None, editor=None, isMinimized=False)

    def get_issue(self, number):
        if number != FIXTURE_NUMBER:
            raise ContractError('offline fixture number required')
        return copy.deepcopy(self.issue)

    def comments(self, number):
        self.get_issue(number)
        return [copy.deepcopy(self.comment)]


def dummy_result(job):
    fields(job, ('fixture_only', 'issue', 'content_sha256'))
    if job['fixture_only'] is not True or job['issue'] != FIXTURE_NUMBER:
        raise ContractError('only fixed offline dummy job supported')
    digest = job['content_sha256']
    if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ContractError('invalid fixture digest')
    return dict(fixture_only=True, issue=FIXTURE_NUMBER, content_sha256=digest,
                checksum=sum(i * i for i in range(100)), execution_allowed=False)


def run_fixture(intake, source, exchange, clock=lambda: FIXTURE_NOW):
    """Manager-only operation; exchange transports one fixed dummy message.

    The durable claim precedes dispatch. Any uncertain/failed dispatch stays
    claimed; automatic retry cannot launch a second dummy. Caller owns lock.
    """
    if type(source) is not FixtureSource:
        raise ContractError('live sources unsupported by fixture route')
    receipt = inspect_once(source, source, intake, FIXTURE_NUMBER, clock)
    db = intake.db
    db.execute('CREATE TABLE IF NOT EXISTS offline_dummy_runs ('
               'issue INTEGER PRIMARY KEY, digest TEXT NOT NULL, phase TEXT NOT NULL, result TEXT)')
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    try:
        old = db.execute('SELECT digest,phase,result FROM offline_dummy_runs WHERE issue=?', (FIXTURE_NUMBER,)).fetchone()
        if old:
            if old[0] != receipt['content_sha256']:
                raise ContractError('frozen dummy plan changed')
            job = dict(fixture_only=True, issue=FIXTURE_NUMBER, content_sha256=old[0])
            if old[1] not in ('CLAIMED', 'BLOCKED', 'COMPLETED'):
                raise ContractError('unknown saved phase')
            if old[1] == 'COMPLETED':
                if old[2] != canonical(dummy_result(job)).decode():
                    raise ContractError('saved result missing or inconsistent')
            elif old[2] is not None:
                raise ContractError('unfinished run has unexpected result')
            db.commit()
            return dict(fixture_only=True, execution_allowed=False, dispatched=False,
                        phase=old[1], duplicate=True)
        db.execute('INSERT INTO offline_dummy_runs VALUES (?,?,?,NULL)',
                   (FIXTURE_NUMBER, receipt['content_sha256'], 'CLAIMED'))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    job = dict(fixture_only=True, issue=FIXTURE_NUMBER, content_sha256=receipt['content_sha256'])
    try:
        # Recheck immediately before transport and after receiving untrusted output.
        inspect_once(source, source, intake, FIXTURE_NUMBER, clock)
        result = exchange(job)
        if type(result) is not dict or canonical(result) != canonical(dummy_result(job)):
            raise ContractError('dummy output does not match the fixed contract')
        inspect_once(source, source, intake, FIXTURE_NUMBER, clock)
        encoded = canonical(result).decode()
        db.execute('UPDATE offline_dummy_runs SET phase=?,result=? WHERE issue=? AND phase=?',
                   ('COMPLETED', encoded, FIXTURE_NUMBER, 'CLAIMED'))
        db.commit()
    except BaseException:
        db.execute('UPDATE offline_dummy_runs SET phase=? WHERE issue=? AND phase=?',
                   ('BLOCKED', FIXTURE_NUMBER, 'CLAIMED'))
        db.commit()
        raise
    return dict(fixture_only=True, execution_allowed=False, dispatched=True,
                phase='COMPLETED', duplicate=False,
                result_sha256=hashlib.sha256(encoded.encode()).hexdigest())
