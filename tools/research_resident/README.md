# Issue92 machine side — deployment is not enabled

This is a **new opt-in runtime**, based on the unmerged PR84 runtime. It does
not change the existing goal/canary entrypoint or migrate any old claim/result.
Do not replace an existing service with it on the strength of offline tests.

## Connection to dot's PR93

The workflow remains owned by PR93. Its reviewed commit is
`9f99f59a5b08b4c7139582b774d17e8c3e2fc753`. The installer reads exactly that
commit's `tools/research_actions/issue_event.py`, verifies SHA256
`d01cc43548dc935c60208319bf0c9beeab8d45df6c03252082e3560359858447`, and installs
it unchanged at `/opt/c3/actions/issue_event.py`. Fetch that Git object before
installation; an absent object is an installation failure, not a download/ref
selected by an event. No workflow file is copied or modified by this change.

The fixed `/opt/c3/bin/submit-issue-event --receipt-stdin` accepts only the
`c3-issue-event-v1` one-line envelope. The manager authenticates its Unix peer
using Linux `SO_PEERCRED`, requiring UID10003. The runner cannot write the
manager's 0700 control directory or SQLite database. Fields in the envelope are
filters, not proof of the GitHub actor. The current Issue/unedited owner approval
is always re-read by the existing formal verifier after dequeue.

ACK follows a `synchronous=FULL` SQLite commit. Repeated receipt IDs return
`duplicate`; `run_attempt` is recorded but excluded from identity. A changed
payload with the same ID is rejected. A later approval's new run ID remains a
new event even after an earlier event was rejected. A committed event survives
a lost ACK. Inbox recovery recovers delivery leases only, never resets a claim.
Cancellation of a runner job after ACK does not revoke a queued request; the
real approval cancellation/expiry checks still apply before and during work.

There is no Issue listing, fallback scan, schedule, or automatic event backfill.
The runner's outbound Actions connection is separate from Issue discovery.
Actions success means delivery only; the manager ledger and actual PR are
independent evidence. Receipt files contain no Issue text or commands.

## One container; three permanent trust roles

PID1 is a root broker limited to SETUID/SETGID. The manager is permanent UID10001,
and the registered runner is permanent UID10003, both without capabilities.
Each admitted job gets a fresh root **subreaper** child of PID1 and Codex at
UID10002. Runner and manager are siblings of the job supervisor, never its
descendants. No nested container, Docker socket, additional capability or host
PID namespace is involved. The receipt executable/client are root-owned in the
read-only image; they never come from an Actions checkout.

The existing owner verifier, claim transaction, IPC rechecks, Codex app-server,
spawn barrier, resource limits and direct-PR result persistence are reused.
The manager calls `process(issue_number)` only. Its receipt thread commits new
events while a model is busy, without cancelling the active job. A new controller
and job supervisor are used per event. The direct-PR installed envelope can admit
separately approved future goals; it is not an execution approval itself.

After success, error, cancellation or timeout, pidfd cleanup signals only the
owned UID10002. The job subreaper also requires **ECHILD**: no live or zombie
descendant, including double forks and escaped sessions. A pair of `/proc` scans
alone is not accepted. Only then are `/work` and UID10002's private-container IPC
objects removed, and cleanup acknowledged before the next event. Manager/runner
are never signal targets. A normal job does not restart or exit the container.

Unverified cleanup, watchdog expiry or loss of a trusted permanent role creates
a durable `resident-blocked` latch and exits this dedicated namespace as a last
resort. On restart the latch refuses new work. A prior uncertain CLAIMED row also
blocks the inbox; no automatic repair or reset is provided. Pending deliveries
can be recovered after an ordinary whole-container restart; claims cannot.

The old disposable supervisor's PID1 requirement and namespace-exit completion
rule remain its default. The new supervisor/validator are selected by trusted
installed Python code, never a profile/Issue boolean.

## Configuration and installer (requires deployment approval)

Use a fresh committed clean checkout and a fresh public deployment ID/profile,
as in `tools/research_goal/DIRECT_PR.md`. Keep the existing risk consents and
model/resource ceilings explicit. Supply dedicated manager GitHub hosts, worker
GitHub token/config and ChatGPT auth via private file arguments outside checkout.
Do not pass tokens as argument values, copy HOME, mount old control volumes, or
use runner registration credentials as worker credentials. Same-owner worker
identity requires the existing explicit owner-identity risk consent; unprotected
main is not made protected by this runtime.

Create a separate protected policy, initially OFF:

```json
{"schema":"c3-resident-intake-v1","enabled":false,"workflow_shas":["REVIEWED_DEFAULT_BRANCH_WORKFLOW_COMMIT_40_HEX"]}
```

`workflow_shas` must contain the actual reviewed default-branch execution SHA(s).
The PR93 source SHA is not automatically the eventual merge/execution SHA.
This policy is loaded once at manager startup, not edited by an incoming event.

Review an official Actions runner Linux x64 release and its published SHA256;
there is deliberately no floating `latest`. The existing official Codex0.160.0
distribution and code-mode-host/bwrap checks are retained in the composed build.

```sh
python3 -B tools/research_resident/prepare.py \
  --approve-new-resident-deployment --approve-worker-github-access \
  --project NEW_UNIQUE_PROJECT --profile PRIVATE_PROFILE --plan PRIVATE_PLAN \
  --intake-policy PRIVATE_OFF_POLICY \
  --codex-auth-file DEDICATED_AUTH --github-hosts-file DEDICATED_HOSTS \
  --worker-pr-config DEDICATED_WORKER_CONFIG \
  --worker-github-token-file DEDICATED_WORKER_TOKEN \
  --runner-version REVIEWED_VERSION --runner-sha256 REVIEWED_SHA256
```

The command builds/creates **one stopped container and two fresh named volumes**
(`/control`, `/runner`), verifies real inspect and exclusivity of both volumes,
and copies/readbacks exact manager-owned 0600 records. It does not start,
register a runner, enable a repository variable, or stop an old service. A
failed installation is left in place; no automatic alternative project is made.
The shared copy helper sends stdin tar without `-a`, retaining its readback
checks. No authentication is printed or saved in the build context.

Aggregate limits stay 1CPU/1GiB/64PID, read-only root, bridge, private PID/IPC and
cgroup namespaces, no-new-privileges, SETUID/SETGID only, no host binds/GPU/socket.
`/work` is the approved <=256MiB noexec tmpfs; IPC socket tmpfs is 1MiB. Codex
per-process CPU/wall/output/work limits are still taken from the formal plan.
The runner uses DOTNET_PROCESSOR_COUNT=1, manager gh uses GOMAXPROCS=1. Whether
the real runner plus Codex fits these unchanged ceilings **needs Docker/live
validation**; exceeding them must not silently increase limits.

## Registration, enablement and cutover — separate approval

1. Review all workflows eligible to run on this repository runner. A custom
   label is routing, not an access-control boundary. Do not expose it to PR code,
   arbitrary shell jobs or unreviewed workflows. Review available repo/org runner
   restrictions before enabling. Runner compromise is not solved by label/receipt
   `workflow_sha` fields. The runner may enqueue hints, never grant approval.
2. Keep PR93's repository variables OFF. Start only this new approved container;
   it waits in `RESIDENT_WAITING_FOR_RUNNER` without model/Issue discovery. Obtain
   a repo-scoped one-time registration token by the owner's authorized procedure.
   Do not expand existing PAT permissions automatically. The official runner
   supports `ACTIONS_RUNNER_INPUT_TOKEN`; the helper takes that token on stdin,
   never argv, and configures this repo, unique name, label `c3-issue-intake-v1`,
   `_work`, unattended, disableupdate, without `--replace` or `--ephemeral`.
3. After explicit registration approval, invoke the installed helper with
   `docker exec -i --user 10003:10003 FULL_ID python -I -B /opt/service/register_runner.py`
   from a private stdin source. No token is echoed. Partial/failed registration
   requires investigation, not implicit replacement. Runner credentials remain
   private in `/runner`; manager credentials remain in `/control`. Registration
   logs are private runner state, not evidence to paste into chat.
4. Test OFF/rejection/commit+ACK/dedup and the resident cleanup diagnostic first.
   Review the installed policy and eligible workflow SHA. An authorized setup
   change to policy plus one setup restart is necessary to enable intake; this
   is not a per-job restart. Then dot enables both repository flags from PR93's
   contract. Do not enable these as part of installer execution.
5. Explicitly identify any old service to stop on cutover, verify full ID/source,
   preserve its volumes/history, and avoid dual dispatch. No old environment is
   stopped/reused by code here. A new owner-approved Issue/model test is separate.

Pinned runners with auto-update disabled require ongoing reviewed updates;
GitHub can stop dispatch to outdated/security-blocked versions. Registration,
repo permissions, credentials and external outbound HTTPS remain operator
responsibilities, not proven by model-free tests.

## Verification boundaries / next proposed test

Run on Linux Python3.10+:

```sh
python3 -B -m unittest discover -s tests -p 'test_harness_*.py'
python3 -B -O -m unittest discover -s tests -p 'test_harness_*.py'
```

Fixtures exercise real SQLite/Unix sockets, the formal approval/claim controller
with model transport replaced, real FD-based deletion, and a real Linux subreaper
adopting a double-fork/setsid child. Fake `/proc`/cgroup cases test refusal logic,
not actual UID/capability/mount isolation. No test invokes registration or Docker.

Next, seek approval for **one model-free dedicated Docker diagnostic**, no real
credentials, no runner registration and no existing volumes: the same limits,
fixed dummy runner/approval fixtures, two distinct sequential dummy jobs, repeated
and unapproved events, abnormal/cancel/timeout cleanup and a fail-closed case.
Bound the diagnostic to 120 seconds after build and build+run to 10 minutes, no
automatic restart/retry; retain only this diagnostic's stopped evidence. Report
same container ID/PID1 ticks, roles, worker0, ECHILD and removed temporary dummy
auth after both jobs. Only afterward propose live runner delivery and real Codex
tests. Unit-test counts alone do not establish deployment or live E2E success.

Official references: [runner implementation/configuration](https://github.com/actions/runner/blob/main/src/Runner.Listener/CommandSettings.cs),
[self-hosted runners](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).
