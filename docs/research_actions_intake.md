# Issue92: Actions event intake (GitHub-side portion)

Status: **default OFF; machine contract not implemented here**. This change is
independent of unmerged PR71/84 and is based on main
`a3c285fb191f95bdf3ecea54408c53adb177b5c1`. See [Issue92](https://github.com/ken2129/c3_neural_compression/issues/92).

## Flow

Issue/comment event → this workflow → resident runner → fixed receipt client →
resident manager's durable inbox → fresh formal approval/claim → Codex → usual PR.
Runner and manager live in the **same one dedicated container**. The manager
serializes work and stays alive after each worker finishes. No issue-list discovery,
schedule trigger, or fallback scan is introduced. Runner's outbound connection to
GitHub is distinct from issue-list polling. Existing PR → dot notification is separate.

This PR supplies the workflow, receipt client, offline tests, and handoff contract.
It does **not** provide a running inbox, resident manager refactor, registration,
credentials, deployment, model test, or proof of end-to-end operation.

## GitHub-side behavior

`.github/workflows/research-issue-intake.yml` accepts owner-generated Issue open,
edit, reopen, close, label changes, and Issue-comment create/edit/delete events.
PR comments and other actors are excluded before a self-hosted job is scheduled.
The job repeats identity checks locally, passes no Issue/comment text to a shell,
uses no checkout/action/download, and requests no `GITHUB_TOKEN` permissions.
The installed client is not loaded from the event's branch or workspace.

No execution label is required here: creation may precede approval, and a later
approval comment must wake the manager. This also means ordinary owner-created
Issues reach the inbox; the manager must reject those without a valid formal request.
Only trusted-owner events wake it. Changes by others still require the existing
in-run approval recheck to notice invalidation. Fresh checks happen on dequeue too.

Job success means **durable notification delivery**, never experiment success or
PR existence. The manager's ledger and actual PR are separate evidence.

## New machine-side interface to implement (not an existing command)

Install this PR's `tools/research_actions/issue_event.py` at the immutable path
`/opt/c3/actions/issue_event.py`; `/usr/bin/python3 -I` executes it. Provide the
following fixed command in the same container:

```
/opt/c3/bin/submit-issue-event --receipt-stdin
```

It reads exactly one UTF-8 JSON object and a newline from stdin. Example:

```json
{
  "schema": "c3-issue-event-v1",
  "repository": "ken2129/c3_neural_compression",
  "repository_id": 1312915009,
  "issue_number": 92,
  "event_name": "issue_comment",
  "action": "created",
  "actor_id": 55587773,
  "comment_id": 123456,
  "run_id": 12345678,
  "run_attempt": 1,
  "workflow_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "receipt_id": "actions:1312915009:12345678"
}
```

For `issues`, `comment_id` is null. Numbers are positive integers less than 2^63;
workflow_sha is a 40-character lowercase Git SHA. The receipt contains no approval
text, credentials, model arguments, filesystem path, shell command, or arbitrary ref.
`workflow_sha` is evidence, not permission to fetch/execute that commit.

Within **30 seconds**, return exit 0 and only this JSON on stdout after durable
commit (or confirmed prior durable commit):

```json
{"schema":"c3-issue-event-ack-v1","receipt_id":"actions:1312915009:12345678","status":"enqueued"}
```

The other valid status is `duplicate`. Max stdout is 4096 bytes. Failures return
nonzero; stderr must contain only safe diagnostics. Do not wait for model completion.
No ACK before durable storage. Lost ACK/timeout is ambiguous; an Actions rerun uses
the same receipt_id, so reconciliation must return duplicate after durable commit.

Manager implementation requirements:
- Authenticated local IPC (for example a protected Unix socket); runner may submit
  receipts only, and cannot mutate ledger, approval, protected configuration or
  manager credentials. Owner/repo fields are filters, **not IPC authentication**.
- Verify schema, repo, IDs, fixed deployed workflow policy, and receipt uniqueness.
  `run_attempt` is recorded, not part of dedup. A new approval event has another
  run_id and must not be discarded merely because that Issue was previously unapproved.
- Durable queue entry and ACK are atomic in meaning. Recover already queued work
  after restart; this is local queue recovery, not GitHub issue discovery. No Actions
  concurrency group is used because pending jobs can replace one another.
- Dequeue serially, fetch the latest specific Issue and formal owner approval,
  validate expiry/content digest/deployment/scope, then use existing claim checks.
  An event is a wake-up hint, never authorization. Different deliveries must not
  bypass the existing per-request claim. Do not reset old BLOCKED claims to retry.
- Continue current in-run approval checks. Cancellation/timeout must clean only
  the owned worker and temporary credentials/workspace, preserve results/claims,
  and keep runner/manager resident. If cleanup is unverified, stop accepting work.
- Runner job cancellation after ACK does not automatically cancel the queued
  research request. Use the real Issue/approval cancellation mechanism; recheck
  before execution and during execution. Never claim otherwise in the UI.
- Retain manager outcomes and PR URLs separately; a notification may be acknowledged
  even when its request is unapproved/rejected. No receipt silently authorizes work.

The existing machine implementation does not yet satisfy this contract just
because `DirectPRController.process(issue_number)` exists. This interface is the
handoff boundary for the machine-side implementation of Issue92.

## Enablement checklist (separate deployment work)

1. Review/merge workflow into the default branch (`main`), where these events run.
   Keep `C3_ISSUE_INTAKE_ENABLED` unset or `false` throughout setup.
2. Implement/test the manager contract and resident lifecycle in the one container.
   Install reviewed, pinned client/CLI bytes outside job/worker-writable paths;
   do not grant runner access to Docker socket, privileged host commands, manager
   secrets, or the worker's writable tree as executable receipt code.
3. Register a dedicated runner with labels `self-hosted`, `linux`,
   `c3-issue-intake-v1` only after separately authorized setup. A custom label is
   routing, not a security boundary. Restrict eligible repository/workflows using
   available runner access controls. A public repo's self-hosted runner can be
   compromised by other workflows/untrusted execution: review all applicable
   workflow access before enabling, and never run PR code on this runner.
4. Verify machine-side OFF behavior, rejection before approval, durable delivery,
   repeated-event/Actions-rerun dedup, and two different approved jobs sequentially
   without container restart; verify cleanup/error/timeout. Model-free and live
   results must be reported separately. Configure the installed manager to accept
   only the reviewed workflow deployment policy.
5. After explicit deployment authorization, set repository variables
   `C3_ISSUE_INTAKE_CONTRACT=c3-issue-event-v1` and
   `C3_ISSUE_INTAKE_ENABLED=true`. These are non-secret flags, not credentials.
   This PR neither sets them nor registers a runner.

With flags absent, jobs are skipped and no self-hosted runner is contacted.
Events missed while disabled or during an outage are **not automatically backfilled**.
Use a deliberate Actions rerun for an existing run, or a new legitimate approval
event; reconcile claim/queue first. There is no fallback issue scan.
GitHub events created using a workflow's `GITHUB_TOKEN` may not trigger another
workflow; validate the actual dot/user credential path in live delivery tests.

## Verification

```
python3 -B -m unittest discover -s tests -p 'test_harness_actions_intake.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_harness_actions_intake.py' -v
```

Tests cover event filtering, unsafe text exclusion, malformed IDs, fixed argv,
ACK failures/timeouts and rerun identity, plus workflow invariants. They do not
prove GitHub scheduling, installed-path permissions, IPC/queue durability, real
approval verification, process isolation, or live Codex operation.

Official references:
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#issue_comment
- https://docs.github.com/en/actions/reference/security/secure-use
- https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency
