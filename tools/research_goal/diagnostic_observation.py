"""Optional historical observation, NEVER an execution-approval callback.

Call once before constructing the diagnostic controller and synthetic receipt.
Its separate read budget is closed before normal admission/rechecks begin. No
observer is invoked by _approval_snapshot, IPC, the model turn, or persistence.
This module neither claims work nor modifies any real Issue/receipt/ledger.
"""
import json
import sys
import time

from research_harness.issue_intake import content_hash, timestamp
from research_harness.manager_github import ManagerGitHub
from research_harness.manager_runtime import RecheckRejected, safe_recheck_failure
from research_harness.owner_approval import (
    GitHubOwnerComments, approval_read_budget, OWNER_ID, OWNER_LOGIN)
from research_harness.schema import loads


class ShadowRead:
    """Observe historical Issue83, including expiry; never grant permission."""
    def __init__(self):
        self.github = ManagerGitHub()
        self.comments = GitHubOwnerComments()

    def observe(self):
        before = self.github.get_issue(83)
        comments = self.comments.comments(83)
        after = self.github.get_issue(83)
        binding_read = self.github.get_issue(83)
        if before != after or after != binding_read:
            raise RecheckRejected('changed')
        digest = content_hash(after)
        matches = []
        for comment in comments:
            author = comment.get('author') or {}
            try:
                body = loads(comment.get('body', ''))
            except Exception:
                continue
            if (author.get('login') == OWNER_LOGIN and author.get('databaseId') == OWNER_ID
                    and comment.get('lastEditedAt') is None and comment.get('editor') is None
                    and comment.get('isMinimized') is False and type(body) is dict
                    and body.get('schema') == 'c3-owner-approval-v1'
                    and body.get('issue') == 83 and body.get('content_sha256') == digest
                    and body.get('decision') == 'approve-execution'):
                matches.append(body)
        if len(matches) != 1:
            raise RecheckRejected('changed')
        return dict(owner_record_matched=True,
                    expired=timestamp(matches[0]['expires_at']) <= time.time())


def _report(record):
    try:
        print(json.dumps(record), file=sys.stderr, flush=True)
    except Exception:
        pass  # A log sink failure does not become an approval decision.


def observe_once(observe):
    """Ordinary observation errors stay outside the authoritative call path.

    The caller supplies only the trusted, bounded ShadowRead operation. Budget
    expiry and normal source errors are observations, not approvals or vetoes.
    Process-control signals still propagate. Existing PID1/UID/reserve/cleanup
    guards remain mandatory; this does not waive an uncertain child cleanup.
    The return value is always None and cannot represent permission.
    """
    try:
        with approval_read_budget(seconds=5):
            value = observe()
            if (type(value) is not dict or set(value) != {'owner_record_matched', 'expired'}
                    or value['owner_record_matched'] is not True or type(value['expired']) is not bool):
                raise ValueError('invalid observation shape')
    except Exception as exc:
        reason = {
            'source_timeout': 'OBSERVATION_TIMEOUT',
            'source_rate_limited': 'OBSERVATION_RATE_LIMITED',
            'source_unavailable': 'OBSERVATION_SOURCE_UNAVAILABLE',
            'source_command_failed': 'OBSERVATION_COMMAND_FAILED',
            'contract': 'OBSERVATION_CONTRACT_REJECTED',
            'changed': 'OBSERVATION_CHANGED',
            'expired': 'OBSERVATION_EXPIRED',
        }.get(safe_recheck_failure(exc)['reason'], 'OBSERVATION_FAILED')
        _report(dict(phase='DIAGNOSTIC_OBSERVATION_FAILED', reason=reason,
                     execution_authorization=False))
    else:
        _report(dict(phase='DIAGNOSTIC_OBSERVATION_COMPLETE', **value,
                     execution_authorization=False))
