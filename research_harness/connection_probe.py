"""One-shot, read-only GitHub connection test. Cannot authorize or run work.

The caller supplies the exact digest of a comment posted after checking a real
owner reply in chat. That check is OUTSIDE this module. GitHub identity alone is
not evidence of chat approval. Every output is explicitly test-only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

from research_harness.schema import ContractError, REPOSITORY, canonical, fields, identifier, loads

PURPOSE = 'chat-github-receipt-only'
PLAN_PATH = 'docs/connection-tests/plan.json'
MAX_BYTES = 262144
HEX40 = re.compile(r'[0-9a-f]{40}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def number(value):
    if type(value) is not int or value <= 0:
        raise ContractError('positive integer required')
    return value


def hex_value(value, pattern):
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ContractError('invalid digest or commit')
    return value


def parse_plan(value):
    fields(value, ('schema', 'purpose', 'request_id', 'revision', 'repository',
                   'expires_at', 'execute'))
    if value['schema'] != 'connection-probe-plan-v1' or value['purpose'] != PURPOSE:
        raise ContractError('not a connection test plan')
    if value['repository'] != REPOSITORY or value['execute'] is not False:
        raise ContractError('execution is forbidden')
    identifier(value['request_id'])
    number(value['revision']); number(value['expires_at'])
    return value


def approval_prompt(plan, pr_number, head):
    """Human-facing exact target; not an authorization operation."""
    parse_plan(plan); number(pr_number); hex_value(head, HEX40)
    return (f"接続試験 {plan['request_id']}（版 {plan['revision']}）: "
            f"PR #{pr_number} / head {head}。GitHubの承認記録を読み取り、"
            "受信確認だけ返します。実験・GPU・マシン操作は行いません。"
            "この接続試験を承認しますか？")


def parse_receipt(value):
    fields(value, ('schema', 'purpose', 'request_id', 'revision', 'repository',
                   'pr_number', 'head_sha', 'plan_digest', 'decision', 'execute'))
    if value['schema'] != 'connection-probe-receipt-v1' or value['purpose'] != PURPOSE:
        raise ContractError('not a connection test receipt')
    if value['repository'] != REPOSITORY or value['execute'] is not False:
        raise ContractError('execution is forbidden')
    if value['decision'] != 'approve-test-only':
        raise ContractError('explicit test approval required')
    identifier(value['request_id']); number(value['revision']); number(value['pr_number'])
    hex_value(value['head_sha'], HEX40); hex_value(value['plan_digest'], HEX64)
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ContractError('redirect prohibited')


class PublicGitHubReader:
    """GET-only public GitHub API reader. No credentials or mutation methods."""
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, suffix):
        # suffixes are constructed below only from validated integers/hex commits.
        request = urllib.request.Request(
            f'https://api.github.com/repos/{REPOSITORY}/{suffix}',
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'c3-connection-probe',
                     'X-GitHub-Api-Version': '2022-11-28'}, method='GET')
        with self.opener.open(request, timeout=15) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ContractError('response too large')
        return loads(raw.decode('utf-8'))


def read_snapshot(reader, pr_number, comment_id):
    number(pr_number); number(comment_id)
    pr = reader.get(f'pulls/{pr_number}')
    head = hex_value(pr['head']['sha'], HEX40)
    blob = reader.get(f'contents/{PLAN_PATH}?ref={head}')
    if blob.get('encoding') != 'base64' or blob.get('path') != PLAN_PATH:
        raise ContractError('unexpected plan file')
    try:
        plan = loads(base64.b64decode(''.join(blob['content'].split()), validate=True).decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ContractError('invalid plan encoding') from exc
    comment = reader.get(f'issues/comments/{comment_id}')
    again = reader.get(f'pulls/{pr_number}')
    def binding(value):
        return (value['number'], value['state'], value['merged'],
                value['head']['sha'], value['head']['repo']['full_name'],
                value['base']['repo']['full_name'])
    if binding(pr) != binding(again):
        raise ContractError('PR changed during readback; inspect again')
    return pr, plan, comment


def acknowledge(db_path, pr_number, comment_id, expected_receipt_digest, reader, *, clock=time.time):
    """Record test receipt once. Supplied digest is an out-of-band caller assertion.

    Never feed this record to Ledger.issue_approval or any execution API.
    No polling, issuer authentication, deployment, subprocess or merge occurs.
    """
    hex_value(expected_receipt_digest, HEX64)
    pr, plan, comment = read_snapshot(reader, pr_number, comment_id)
    parse_plan(plan)
    if pr.get('number') != pr_number or pr.get('state') != 'open' or pr.get('merged') is not False:
        raise ContractError('test PR must remain open and unmerged')
    if pr['base']['repo']['full_name'] != REPOSITORY or pr['head']['repo']['full_name'] != REPOSITORY:
        raise ContractError('unexpected repository')
    if clock() >= plan['expires_at']:
        raise ContractError('test plan expired')
    if comment.get('id') != comment_id or comment.get('issue_url') != f'https://api.github.com/repos/{REPOSITORY}/issues/{pr_number}':
        raise ContractError('comment does not belong to test PR')
    if type(comment.get('created_at')) is not str or comment.get('created_at') != comment.get('updated_at'):
        raise ContractError('edited receipt rejected')
    receipt = parse_receipt(loads(comment['body']))
    if digest(receipt) != expected_receipt_digest:
        raise ContractError('receipt differs from owner-confirmed target')
    for key in ('request_id', 'revision', 'repository'):
        if receipt[key] != plan[key]:
            raise ContractError('receipt does not match plan')
    if receipt['pr_number'] != pr_number or receipt['head_sha'] != pr['head']['sha'] or receipt['plan_digest'] != digest(plan):
        raise ContractError('stale PR head or plan')
    # A small dedicated store, not the execution Ledger. Atomic duplicate handling.
    result = {'schema': 'connection-probe-ack-v1', 'purpose': PURPOSE,
              'request_id': plan['request_id'], 'revision': plan['revision'],
              'pr_number': pr_number, 'head_sha': pr['head']['sha'],
              'comment_id': comment_id, 'receipt_digest': expected_receipt_digest,
              'status': 'RECEIVED_TEST_ONLY', 'execute': False}
    with sqlite3.connect(db_path, timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS probe_receipts (request_id TEXT PRIMARY KEY, payload BLOB NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        if clock() >= plan['expires_at']:
            raise ContractError('test plan expired before recording')
        old = db.execute('SELECT payload FROM probe_receipts WHERE request_id=?', (plan['request_id'],)).fetchone()
        encoded = canonical(result)
        if old and old[0] != encoded:
            raise ContractError('request already acknowledged with a different receipt')
        if not old:
            db.execute('INSERT INTO probe_receipts VALUES (?,?)', (plan['request_id'], encoded))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr', required=True, type=int)
    parser.add_argument('--comment', required=True, type=int)
    parser.add_argument('--confirmed-receipt-digest', required=True)
    parser.add_argument('--receipt-db', required=True, type=Path)
    args = parser.parse_args()
    result = acknowledge(args.receipt_db, args.pr, args.comment, args.confirmed_receipt_digest,
                         PublicGitHubReader())
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
