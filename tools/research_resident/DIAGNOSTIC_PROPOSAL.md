# Proposed model-free Docker diagnostic — not yet run

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
