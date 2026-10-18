"""Check an unedited owner approval against an Issue; never execute anything.

Trusts GitHub's authenticated author/edit metadata, not fields claimed by a
comment. The machine worker must have no owner credentials or manager access.
That boundary is NOT established by this module; live execution stays disabled.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import math
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

from .issue_intake import (Intake, PublicGitHub, classify, content_hash,
                           positive_int, timestamp)
from .schema import ContractError, REPOSITORY, canonical, fields, loads
from .store import SingleInstance

_READ_DEADLINE = ContextVar('owner_approval_read_deadline', default=None)


@contextmanager
def approval_read_budget(seconds=5.0):
    """Trusted per-refresh shared budget; never renew a nested deadline."""
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 5:
        raise ContractError('invalid approval read budget')
    deadline = time.monotonic() + seconds
    existing = _READ_DEADLINE.get()
    if existing is not None:
        deadline = min(deadline, existing)
    token = _READ_DEADLINE.set(deadline)
    try:
        yield
        approval_read_timeout(seconds)
    finally:
        _READ_DEADLINE.reset(token)


def approval_read_timeout(default):
    deadline = _READ_DEADLINE.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError('approval read budget exhausted')
    return min(default, remaining)


OWNER_ID = 55587773
OWNER_LOGIN = 'ken2129'
SCHEMA = 'c3-owner-approval-v1'
MAX_BYTES = 2 * 1024 * 1024
QUERY = '''query($number:Int!, $cursor:String) {
  repository(owner:"ken2129", name:"c3_neural_compression") {
    issue(number:$number) {
      number
      comments(first:100, after:$cursor) {
        nodes { id body createdAt updatedAt lastEditedAt isMinimized
          author { login ... on User { databaseId } }
          editor { login }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}'''


class MissingApproval(ContractError):
    pass


class RevokedApproval(ContractError):
    pass


class GitHubOwnerComments:
    """Read-only GraphQL using existing gh authentication; never signs in.

    Runs the installed gh executable with a fixed query, not Issue-supplied
    commands. The gh credential/config must belong only to the trusted manager.
    """
    def comments(self, issue):
        positive_int(issue)
        result, cursor, seen = [], None, set()
        started = time.monotonic()
        for _ in range(20):
            if time.monotonic() - started > 120:
                raise ContractError('comment scan deadline exceeded')
            command = ['gh', 'api', '--hostname', 'github.com', 'graphql', '-f', 'query=' + QUERY, '-F', f'number={issue}']
            if cursor is not None:
                command += ['-f', 'cursor=' + cursor]
            # Timeout applies to the gh process; no remote errors are echoed.
            if _READ_DEADLINE.get() is not None:
                # Live periodic refresh uses the same bounded, manager-only
                # process/output/cleanup path as REST, not communicate().
                approval_read_timeout(20)
                from .manager_github import ManagerGitHub, ManagerGitHubCommandFailed
                command[0] = ManagerGitHub._executable()
                code, output = ManagerGitHub._run(command)
                if code != 0:
                    raise ManagerGitHubCommandFailed('GitHub manager comment command failed')
            else:
                response = subprocess.run(command, capture_output=True, timeout=20, check=True)
                output = response.stdout
            if len(output) > MAX_BYTES:
                raise ContractError('GraphQL response too large')
            try:
                data = loads(output)
            except (ContractError, ValueError, UnicodeError, RecursionError, TypeError):
                from .manager_github import report_read_failure
                report_read_failure('GRAPHQL_JSON_INVALID')
                raise
            if data.get('errors'):
                from .manager_github import report_read_failure
                report_read_failure('GRAPHQL_ERRORS')
                raise ContractError('GraphQL returned errors')
            try:
                item = data['data']['repository']['issue']
                if item['number'] != issue:
                    raise ContractError('Issue identity mismatch')
                page = item['comments']
                nodes, info = page['nodes'], page['pageInfo']
                if type(nodes) is not list or len(nodes) > 100 or type(info['hasNextPage']) is not bool:
                    raise ContractError('invalid comments page')
                result.extend(nodes)
                if not info['hasNextPage']:
                    return result
                cursor = info['endCursor']
                if type(cursor) is not str or not cursor or len(cursor) > 512 or cursor in seen:
                    raise ContractError('invalid/repeated pagination cursor')
                seen.add(cursor)
            except (KeyError, TypeError) as exc:
                from .manager_github import report_read_failure
                report_read_failure('GRAPHQL_METADATA_INVALID')
                raise ContractError('missing comment metadata') from exc
        raise ContractError('comment pagination exceeded')


def matching_receipt(issue, comments, now):
    status, request = classify(issue, now)
    if status != 'PENDING_TRUSTED_APPROVAL' or request['issuer'] != OWNER_LOGIN:
        raise ContractError('Issue is not an eligible owner-issued request')
    if type(comments) is not list:
        raise ContractError('comments array required')
    matches = []
    invalid = False
    for comment in comments:
        if type(comment) is not dict:
            raise ContractError('invalid comment metadata')
        author = comment.get('author')
        if type(author) is not dict or author.get('databaseId') != OWNER_ID or author.get('login') != OWNER_LOGIN:
            continue
        try:
            body = loads(comment['body'])
        except (ContractError, TypeError, KeyError, RecursionError):
            continue
        if type(body) is not dict or body.get('schema') != SCHEMA or body.get('request_id') != request['approval_ref']:
            continue
        try:
            required_meta = {'id', 'body', 'createdAt', 'updatedAt', 'lastEditedAt', 'isMinimized', 'author', 'editor'}
            if not required_meta <= comment.keys():
                raise ContractError('approval edit metadata unavailable')
            if type(comment['id']) is not str or not comment['id'] or len(comment['id']) > 200:
                raise ContractError('invalid comment node identity')
            timestamp(comment['createdAt']); timestamp(comment['updatedAt'])
            if (comment['lastEditedAt'] is not None or comment['editor'] is not None
                    or comment['isMinimized'] is not False):
                raise ContractError('edited/hidden approval rejected')
            fields(body, ('schema', 'request_id', 'repository', 'issue', 'plan_version',
                          'content_sha256', 'expires_at', 'decision'))
            if (body['repository'] != REPOSITORY or type(body['issue']) is not int or body['issue'] != issue['number']
                    or type(body['plan_version']) is not int or body['plan_version'] != request['plan_version']
                    or body['content_sha256'] != content_hash(issue)
                    or body['expires_at'] != request['expires_at']):
                raise ContractError('approval binding mismatch')
            if timestamp(body['expires_at']) <= now:
                raise ContractError('approval expired')
            if body['decision'] == 'cancel-execution':
                raise RevokedApproval('execution approval revoked')
            if body['decision'] != 'approve-execution':
                raise ContractError('not an execution approval')
            matches.append((comment, body))
        except RevokedApproval:
            raise
        except ContractError:
            invalid = True
    if invalid:
        raise ContractError('invalid matching approval record')
    if not matches:
        raise MissingApproval('owner approval not yet present')
    if len(matches) != 1:
        raise ContractError('exactly one unedited owner approval required')
    return matches[0]


def inspect_once(client, comments_client, intake, number, clock):
    """Read twice and persist the reviewed receipt, with no executable transition."""
    before = client.get_issue(number)
    comments = comments_client.comments(number)
    after = client.get_issue(number)
    if before != after:
        raise ContractError('Issue changed during inspection')
    now = clock()
    rows = intake.observe_batch([after], now)
    if len(rows) != 1 or rows[0]['status'] != 'PENDING_TRUSTED_APPROVAL':
        raise ContractError('intake requires reconciliation')
    db = intake.db
    db.execute('CREATE TABLE IF NOT EXISTS owner_receipts ('
               'issue INTEGER PRIMARY KEY, comment TEXT NOT NULL UNIQUE, '
               'receipt_hash TEXT NOT NULL, content_hash TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS owner_approval_blocks (issue INTEGER PRIMARY KEY)')
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('SELECT issue FROM owner_approval_blocks WHERE issue=?', (number,)).fetchone():
            raise ContractError('previous approval invalidated; reconcile explicitly')
        previous = db.execute('SELECT comment,receipt_hash,content_hash FROM owner_receipts WHERE issue=?', (number,)).fetchone()
        try:
            comment, body = matching_receipt(after, comments, now)
        except ContractError as exc:
            if previous is not None or not isinstance(exc, MissingApproval):
                db.execute('INSERT OR IGNORE INTO owner_approval_blocks VALUES (?)', (number,))
                db.commit()
            raise
        receipt_hash = hashlib.sha256(canonical(comment)).hexdigest()
        wanted = (comment['id'], receipt_hash, content_hash(after))
        if previous is not None and previous != wanted:
            db.execute('INSERT OR IGNORE INTO owner_approval_blocks VALUES (?)', (number,))
            db.commit()
            raise ContractError('frozen approval changed; explicit reconciliation required')
        db.execute('INSERT OR IGNORE INTO owner_receipts VALUES (?,?,?,?)', (number, *wanted))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return dict(issue=number, comment=comment['id'], approval='OWNER_RECORD_MATCHED',
                content_sha256=content_hash(after), duplicate=previous is not None,
                execution_allowed=False, blocker='WORKER_ISOLATION_AND_RUNTIME_NOT_VERIFIED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issue', type=int, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    try:
        positive_int(args.issue)
        with SingleInstance(str(args.state) + '.lock'):
            intake = Intake(args.state)
            try:
                result = inspect_once(PublicGitHub(), GitHubOwnerComments(), intake, args.issue,
                                      lambda: datetime.now(timezone.utc).timestamp())
            finally:
                intake.close()
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({'execution_allowed': False, 'error': 'approval not verified; check source and manager setup'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
