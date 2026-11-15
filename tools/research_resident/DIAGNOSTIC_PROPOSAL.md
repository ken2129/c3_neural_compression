# Model-free Docker diagnostic — PASSED on fixed runtime

The later explicit instruction to continue within the same limits authorized
the subsequent bounded attempts below. No production cutover, credentials,
real runner registration, model call or GitHub operation was included.

## Successful Docker observation

Fixed runtime: `abee7bf5debde263cc83b041f62addb496948d79`.

- Project: `uchishiba-resident-diag-f27ba71e`.
- Container: `9a005e9270e728871a31c8819f77fa7f176527bfeeb4537594736fdc55210a17`.
- Image: `sha256:d9967ebe5a360e74d617c7ef43951c72025cfbee1ed4218f7d29e843788f4f24`.
- Result: `RESIDENT_DIAGNOSTIC_PASSED`, all **18** saved checks true.
- Run monitor: **9.412 seconds**; build/create/run/stop: **11.992 seconds**.
- At completion: running, RestartCount=0, same PID1/manager/runner identities,
  worker0 and empty work area. PID1 start ticks: 556020944.
- IPC trace: `approved_false → goal_failure → failure_ack → job_finished → cleaned`.
- Synthetic A (920001): BLOCKED, no result; synthetic B (920002):
  WORKER_FINISHED, completed result saved, pr_url=null. Both re-notifications
  returned duplicate=true/dispatched=false. Two claims were preserved.
- Actual UID/capability/resource/mount/admission checks, permanent worker UID
  drop, pidfd/subreaper/ECHILD recovery of the setsid/double-fork descendant,
  temporary dummy-auth/workspace/IPC removal and SQLite persistence ran in Docker.
- Only after those live assertions, the host stopped this exact container.
  Final status exited, exit143 (the requested SIGTERM shutdown), RestartCount=0,
  PID0, OOM=false, AppArmor docker-default. The planned stop is separate from
  the successful running-state diagnostic result.
- The saved result is UID/GID10001:10001, mode0600. Its SHA256 is
  `e8887c272b18a0c96bd9e513435933cd1ac09f2a998dfb07d2a80b2e54aa4f0c`.
  A separate stopped-container readback verified every check and the checksum;
  mode=ro/query_only SQLite independently confirmed both phases/result presence
  and two claims. Both volumes have only this container as consumer. Installed
  launcher, resident runtime and runner-bootstrap bytes match the fixed commit.

### Kernel-context defect found and fixed during the continued diagnosis

After the runner startup fix, the manager reached RESIDENT_MANAGER_READY but
the job failed at reserve. Allowlisted code-site/role/errno diagnostics pinned
the error to `uid_supervisor.py:193`, reading `/proc/1/ns/pid`, errno13 in the
job role. PID1's temporary UID switches make cross-process namespace links
unreadable to the restricted job root; neither SYS_PTRACE nor a dumpability
relaxation was added.

PID1 now sends its own live epoch and IPC namespace over its private job
bootstrap pipe. The resident backend compares the job's actual self namespace,
boot ID, root PID1 identity and PID1 start ticks against that anchor. IPC
admission keeps comparing the actual self IPC namespace. These anchors never
come from an Issue, event, worker or mutable configuration. The ordinary
disposable backend retains its direct PID1 observation; no checks are skipped.

The regression uses real Linux self/boot/PID1-stat observations while rejecting
cross-process namespace-link reads, then rejects mismatched namespace, boot,
ticks, malformed anchors and IPC. Only the absent image marker is simulated.
Together with all earlier tests, Python3.10.12 normal and optimized runs each
passed **1087 tests** before the successful Docker attempt.

### What this result does and does not establish

This establishes the model-free native Docker lifecycle from formal synthetic
approval rejection through actual cleanup and the next job's persisted result,
without restarting PID1/manager/runner. GitHub snapshots, runner delivery and
Codex app-server replies remain fixed local fixtures. The official Actions
runner distribution/registration, real Codex communication and inference,
production resource fit, GitHub PR publication and dot notification are still
unverified. No production service was deployed or switched over.

The successful container/image/two volumes and earlier failed attempts remain
stopped and retained. No host-side log archive or real authentication copy was
created by these attempts. The sections below preserve the original scope and
earlier failures rather than treating them as successful reproductions.

The approval-rejection fix must first pass the Linux regression and be pinned
to a reviewed full PR94 commit. This document is a proposed operation, not
permission to start Docker, register a runner or transfer credentials.

## Scope and limits

- One fresh unique project, one new image, one container, two empty named
  volumes (`/control`, `/runner`). Do not mount/reuse any existing volume, socket,
  HOME, authentication, experiment input or old ledger. Build has outbound
  download traffic and disk use. The source and test entrypoint must be fixed
  and reviewable before approval/execution; no event/Issue selects code or refs.
- One start, `restart=no`, no retry. Host wall-clock run deadline **120 seconds**,
  build+run deadline **600 seconds**. On deadline stop only this diagnostic's
  full verified container ID. CPU ulimit is not the wall-clock deadline.
- Keep 1CPU/1GiB/64PID, read-only root, private PID/IPC/cgroup, bridge,
  no-new-privileges, cap_drop ALL / cap_add SETUID+SETGID only, docker-default
  AppArmor/default seccomp, <=256MiB noexec work tmpfs, no bind/GPU/host socket.
  Never increase limits or relax ownership/security checks on failure.
- No real credentials or registration. Dummy files are explicitly synthetic.
  No Codex/model inference, GitHub API write/read, real Issue/approval/claim,
  research PR, notification, merge or existing-service switchover.

## Route and assertions

Use the real resident manager-core/controller, PID1 broker, job supervisor,
UID10001/10002/10003 and cleanup code. Substitute only fixed local GitHub
snapshots, runner delivery and Codex app-server responses. Native ownership,
capabilities, resource/mount checks, approval parsing, SQLite, private IPC,
pidfd/subreaper cleanup and temporary-file removal must actually run.

1. Enqueue two different synthetic requests through the receipt IPC. Start A
   with dummy child processes including double-fork/setsid and a dummy auth file.
2. Cancel A's synthetic approval. Observe negative recheck, owned descendants
   ECHILD/workspace empty, `goal_failure`, durable BLOCKED, `failure_ack`,
   `job_finished`, `cleaned`, in that order. Do not clear A's claim.
3. Accept B through the same manager and produce a small fixed dummy result.
   Require WORKER_FINISHED, saved result and complete cleanup. Re-deliver A and B
   to verify neither dispatches again. No model or PR is involved.
4. Verify unchanged container ID, PID1 start ticks, manager/runner identities,
   RestartCount=0, worker0, and no dummy worker authentication/workspace after
   both jobs. Record ledger state and fixed phase/reason/stage only.

The test-only entrypoint/deployment validation must explicitly require the
diagnostic identity and `restart=no`. Production validation remains strict
(`unless-stopped`); do not fabricate/normalize inspect to pass it. The exact
diagnostic changes and fixed commit will be supplied before the start approval.

The single approved attempt at `5dbc70431f0bb12cfea561abccc7c4e55bb8d8d4`
built and created the diagnostic, then stopped at inspect validation. Start
count was zero, RestartCount zero, status `created`; no A/B job ran. All shared
identity/resource/mount predicates accepted the unmodified measured inspect.
The diagnostic-only check wrongly required `AppArmorProfile=docker-default`
before start; Docker 28.3.2 reported an empty applied profile at that point.
The created container, image and two dedicated volumes remain as evidence.
This is a diagnostic-validator failure, not a reproduction of cancellation.

The correction explicitly requests `apparmor=docker-default`. Only an actual
`created`, non-running, PID-zero inspect may have an empty applied profile;
this means pending activation, not verified enforcement. Once started, the
host monitor requires `AppArmorProfile=docker-default`, and every diagnostic
role checks `/proc/self/attr/current` for `docker-default (enforce)` before
doing work. Production validation and all resource/ownership checks stay as
before. Offline lifecycle regressions do not establish Docker success.
The original attempt is over; this correction does not authorize a retry.

## Second approved attempt: runner startup blocked

At `6ee756a7ccde7fcf88810fc4ffd8eab3eae02051`, the separately approved
attempt passed deployment validation and started once. The container exited
125 with `DIAGNOSTIC_CONTRACT_REJECTED`, error_class `permission`, and
RestartCount=0. Docker reported the applied `docker-default` profile and the
explicit profile/NNP options. The run monitor took 1.191 seconds; the whole
attempt took 3.760 seconds. No A/B job or saved result was reached.

The stopped volume has the runner's private `home`, `_temp`, `_diag` and empty
output log, but no manager state/inbox/claim/result. This places the failure
after private runner preparation. The native launch used `Popen(cwd='/runner',
user=10003)` after restoring root, without a DAC override capability. CPython
performs chdir before setgid/setuid, so root cannot enter UID10003's 0700
directory. This is a concrete startup defect consistent with the saved state;
the original error did not record its filename/syscall, so that exact syscall
has not been directly observed in the failed container.

The correction starts a fixed trusted Python bootstrap with cwd `/`, drops
UID/GID/groups through Popen, then checks permanent UID10003, zero capabilities
and NNP before entering the still-private runner directory and execing the
runner in the same PID. No capability or permission is added. Model-free tests
exercise real chdir denial, private-file rejection, actual exec/cwd/PID, plus
simulated Linux identity facts. Actual root-to-10003 startup and the subsequent
A/B lifecycle remain unverified until another separately approved Docker test.
No automatic retry or change to this stopped environment is made.

The fixed implementation is now `run_diagnostic.py`, `diagnostic.py`,
`diagnostic_process.py`, `Dockerfile.diagnostic`, and `compose.diagnostic.yaml`.
The separately named diagnostic validator requires the actual diagnostic
entrypoint/purpose, restart=no and docker-default. It shares all other resource
and mount checks with production; inspect objects are never rewritten.

```sh
python3 -B tools/research_resident/run_diagnostic.py --approve-model-free-diagnostic
```

This builds a minimal Python fixture image, **not the official Codex/Actions
runner distribution**. Only their wire responses/delivery are simulated. It
has no credential-file arguments: dummy noncredential literals are created
locally. The normal root broker/manager/job-supervisor, staging ownership,
native admission, UID drop/barrier, bounded transport, formal approval parser,
claims, pidfd/ECHILD and workspace/IPC deletion actually run. No production
registration, network read, model call or publication is exercised. The user
must explicitly authorize this one attempt; the CLI flag is not a new approval.

## Completion and remaining gaps

Stop and retain only the new diagnostic container/volumes and small fixed
diagnostic evidence; no host-side copy of secrets or log archive. Record the
live completion assertions separately from the subsequent manual stop/exit.
Failure or deadline ends this one attempt; no automatic new project or retry.

Passing this would establish the model-free resident lifecycle in Docker.
Actual Actions runner registration/delivery, real Codex/model communication,
PR publication and dot notification would still be unverified and would need a
separate concrete approval. Existing services and all historical evidence remain
unchanged throughout this proposal.
