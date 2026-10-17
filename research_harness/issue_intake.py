"""Read-only public Issue intake. No approval authority and no execution API."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .schema import ContractError, REPOSITORY, canonical, fields, identifier, loads
from .store import SingleInstance

PREFIX = '[実行承認済み]'
API = f'https://api.github.com/repos/{REPOSITORY}'
MAX_RESPONSE = 2 * 1024 * 1024
MAX_PAGES = 20
MAX_TRACKED = 200
SCAN_SECONDS = 120
MARKER = '\n```c3-execution-request\n'


def timestamp(value):
    if type(value) is not str or not value.endswith('Z'):
        raise ContractError('UTC timestamp required')
    try:
        return datetime.fromisoformat(value[:-1] + '+00:00').timestamp()
    except ValueError as exc:
        raise ContractError('invalid timestamp') from exc


def positive_int(value):
    if type(value) is not int or value < 1:
        raise ContractError('positive integer required')
    return value


def snapshot(data):
    """Validate GitHub identity before interpreting any user-controlled text."""
    if type(data) is not dict:
        raise ContractError('Issue object required')
    number = positive_int(data.get('number'))
    if data.get('url') != f'{API}/issues/{number}' or data.get('repository_url') != API:
        raise ContractError('Issue repository/number mismatch')
    title, body = data.get('title'), data.get('body') or ''
    if type(title) is not str or type(body) is not str or len(body.encode()) > 100000:
        raise ContractError('invalid Issue text')
    user = data.get('user')
    if type(user) is not dict:
        raise ContractError('missing Issue author')
    author = positive_int(user.get('id'))
    if data.get('state') not in ('open', 'closed'):
        raise ContractError('invalid Issue state')
    timestamp(data.get('updated_at'))
    return dict(number=number, author_id=author, title=title, body=body,
                state=data['state'], updated_at=data['updated_at'],
                is_pr='pull_request' in data)


def content_hash(issue):
    # State/updated_at are observed separately; content and author bind the revision.
    return hashlib.sha256(canonical({k: issue[k] for k in
        ('number', 'author_id', 'title', 'body')})).hexdigest()


def classify(issue, now):
    if issue['is_pr'] or not issue['title'].startswith(PREFIX):
        return 'INELIGIBLE', None
    if issue['state'] != 'open':
        return 'CLOSED_REQUIRES_RECONCILIATION', None
    try:
        body = issue['body']
        if body.count(MARKER) != 1:
            raise ContractError('one request block required')
        before, encoded = body.split(MARKER)
        encoded, after = encoded.split('\n```', 1)
        if after.strip():
            raise ContractError('request block must be last')
        request = loads(encoded)
        fields(request, ('schema_version', 'plan_version', 'issuer', 'approval_ref',
                         'expires_at', 'cancelled', 'plan'))
        if type(request['schema_version']) is not int or request['schema_version'] != 1:
            raise ContractError('unsupported request version')
        positive_int(request['plan_version'])
        identifier(request['issuer']); identifier(request['approval_ref'])
        if type(request['cancelled']) is not bool or type(request['plan']) is not dict or not request['plan']:
            raise ContractError('invalid request fields')
        expires = timestamp(request['expires_at'])
    except (ContractError, ValueError, TypeError, KeyError, RecursionError):
        return 'INVALID_REQUEST', None
    # These declarations can only make intake more restrictive, never authorize it.
    if request['cancelled']:
        return 'CANCELLED_DECLARED', request
    if expires <= now:
        return 'EXPIRED', request
    return 'PENDING_TRUSTED_APPROVAL', request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ContractError('GitHub redirect rejected')


class PublicGitHub:
    """Unauthenticated GET only, fixed repository, bounded responses and pagination."""
    def __init__(self):
        self.opener = build_opener(NoRedirect())

    def _get(self, suffix):
        request = Request(API + suffix, headers={
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'c3-issue-intake-readonly'})
        with self.opener.open(request, timeout=20) as response:
            data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ContractError('GitHub response too large')
        return loads(data.decode('utf-8'))

    def get_issue(self, number):
        number = positive_int(number)
        issue = snapshot(self._get(f'/issues/{number}'))
        if issue['number'] != number:
            raise ContractError('unexpected Issue number')
        return issue

    def list_candidates(self):
        found = {}
        started = time.monotonic()
        for page in range(1, MAX_PAGES + 1):
            if time.monotonic() - started >= SCAN_SECONDS:
                raise ContractError('listing deadline exceeded')
            items = self._get(f'/issues?state=open&sort=created&direction=asc&per_page=100&page={page}')
            if type(items) is not list or len(items) > 100:
                raise ContractError('invalid GitHub page')
            for raw in items:
                issue = snapshot(raw)
                if not issue['is_pr'] and issue['title'].startswith(PREFIX):
                    found[issue['number']] = issue
            if len(items) < 100:
                return found
        raise ContractError('pagination limit reached; scan incomplete')


class Intake:
    """Observation journal only. Never inserts into the executable fixture ledger.

    Keep this DB in the trusted manager's private state directory. SQLite and
    flock provide consistency, not isolation from a worker with the same UID.
    """
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS intake (number INTEGER PRIMARY KEY, '
                        'fingerprint TEXT NOT NULL, status TEXT NOT NULL, '
                        'changed INTEGER NOT NULL, observations INTEGER NOT NULL, observed_status TEXT NOT NULL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def tracked(self):
        return [row[0] for row in self.db.execute('SELECT number FROM intake ORDER BY number')]

    def observe_batch(self, issues, now):
        # Full retrieval precedes commit; network failure cannot be an empty queue.
        result = []
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for issue in issues:
                number = issue['number']
                status, request = classify(issue, now)
                observed_status = status
                fingerprint = content_hash(issue)
                old = self.db.execute('SELECT fingerprint,status,changed,observations,observed_status FROM intake WHERE number=?',
                                      (number,)).fetchone()
                if old is None and status == 'INELIGIBLE':
                    continue
                changed = bool(old and (old[2] or old[0] != fingerprint or
                    old[1] in ('CLOSED_REQUIRES_RECONCILIATION', 'EXPIRED', 'CANCELLED_DECLARED')))
                if changed:
                    status = 'CHANGED_REQUIRES_REAPPROVAL'
                observations = (old[3] if old else 0) + 1
                self.db.execute('INSERT OR REPLACE INTO intake VALUES (?,?,?,?,?,?)',
                                (number, fingerprint, status, int(changed), observations, observed_status))
                result.append(dict(issue=number, content_sha256=fingerprint, status=status,
                                   duplicate=bool(old and old[0] == fingerprint and old[1] == status and old[4] == observed_status),
                                   observed_status=observed_status, requires_reapproval=changed,
                                   plan_version=request['plan_version'] if request else None,
                                   execution_allowed=False))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return result


def poll_once(client, intake, now):
    started = time.monotonic()
    candidates = client.list_candidates()
    # Re-read known Issues individually so prefix removal and closure aren't lost.
    numbers = sorted(set(candidates) | set(intake.tracked()))
    if len(numbers) > MAX_TRACKED:
        raise ContractError('tracked Issue limit reached; reconcile/archive history explicitly')
    issues = []
    for number in numbers:
        if time.monotonic() - started >= SCAN_SECONDS:
            raise ContractError('scan deadline exceeded')
        issues.append(client.get_issue(number))
    if time.monotonic() - started >= SCAN_SECONDS:
        raise ContractError('scan deadline exceeded')
    return intake.observe_batch(issues, now() if callable(now) else now)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', required=True)
    parser.add_argument('--state', type=Path, required=True,
                        help='private local SQLite observation database')
    args = parser.parse_args()
    try:
        with SingleInstance(str(args.state) + '.lock'):
            intake = Intake(args.state)
            try:
                result = poll_once(PublicGitHub(), intake, lambda: datetime.now(timezone.utc).timestamp())
            finally:
                intake.close()
        print(json.dumps({'mode': 'intake-only', 'execution_allowed': False,
                          'candidates': result}, ensure_ascii=False))
    except Exception:
        # Do not echo remote body, URLs, credentials, or database paths into logs.
        print(json.dumps({'mode': 'intake-only', 'execution_allowed': False,
                          'error': 'intake failed; retry after checking source and state'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
