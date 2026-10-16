"""One bounded, synthetic CPU round trip; no live operations or credentials."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import resource
import shutil
import time

from research_harness.artifacts import (EvidenceBinding, MockArtifactValidator,
                                        canonical_bytes, freeze_artifacts, render_public_result)
from research_harness.bridge import FakeChatBridge
from research_harness.process import ProcessManager
from research_harness.publication import (CheckResult, FakeGitHubTransport, FakePublisher,
                                          MergeAuthorization, MergePolicy, PRSnapshot)
from research_harness.schema import ContractError, fixture_plan, freeze_source, new_salt
from research_harness.store import Ledger, SingleInstance


def capabilities():
    return {'schema_version': 1, 'mode': 'fixture-only', 'cpu_fixture': 'available-linux',
            'live_execution': 'BLOCKED_ENV', 'chat_bridge': 'fake-only',
            'trusted_issuer': 'fake-only', 'protected_validator': 'unsupported',
            'worker_isolation': 'unsupported', 'gpu_device_enforcement': 'unsupported',
            'shared_machine_allocation': 'unverified', 'hard_resource_limits': 'unsupported',
            'codex_worker': 'unsupported', 'github_publisher': 'fake-only',
            'box_transfer': 'fake-only', 'local_cleanup': 'not-implemented'}


def _journal(path, value):
    temporary = path.with_suffix('.tmp')
    # Dedicated private fixture directory, single-instance owner.
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def demo(root):
    """Run once in a NEW directory. Fake issue/PR IDs never leave this process."""
    root = Path(root)
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    with SingleInstance(root / 'dispatcher.lock'):
        plan = replace(fixture_plan(), issue=900001)
        salt = new_salt()
        plan_digest = plan.digest(salt)
        ledger = Ledger(root / 'ledger.sqlite3')
        ledger.register_plan(plan, digest=plan_digest)
        bridge = FakeChatBridge()
        bridge.send('plan-event', plan.request_id, 'plan',
                    {'fixture_only': True, 'plan_id': plan.plan_id, 'revision': plan.revision,
                     'digest': plan_digest, 'operation': plan.operation})
        bridge.readback('plan-event')
        now = time.time()
        approval = {'version': 1, 'event_id': 'approval-event', 'issuer_id': 'fake-issuer',
                    'request_id': plan.request_id, 'plan_id': plan.plan_id,
                    'revision': plan.revision, 'repo': plan.repo, 'issue': plan.issue,
                    'digest': plan_digest, 'nonce': 'fixture-nonce', 'scope': ['cpu_fixture'],
                    'issued_at': now, 'expires_at': now + 60}
        bridge.send('approval-event', plan.request_id, 'approval', approval)
        ledger.ingest_approval(bridge.readback('approval-event')[1])
        claim = ledger.claim(plan.request_id, owner='fixture-controller', now=time.time())
        if shutil.disk_usage(root).free < plan.budget.min_free_bytes:
            ledger.transition(claim.run_id, claim.fence, 'S03', 'resource_missing', now=time.time())
            raise ContractError('BLOCKED_RESOURCE: fixture disk reserve unavailable')
        frozen = freeze_source(plan, '1' * 40, list(plan.allowed_paths), independently_validated=True)
        _journal(root / 'execution-manifest.json', frozen)
        ledger.transition(claim.run_id, claim.fence, 'S05', 'candidate_valid', now=time.time())
        manager = ProcessManager(root / 'processes')
        started = time.monotonic()
        cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)

        def admit(receipt):
            identity = receipt['identity']
            ledger.mark_running(claim.run_id, claim.fence, now=time.time(),
                                process={'pid': identity['pid'], 'start_time': identity['start_ticks'],
                                         'boot_id': identity['boot_id'], 'process_group': identity['pgrp']})

        try:
            manager.launch(claim.attempt_id, duration=0.01, timeout=plan.budget.wall_seconds,
                           before_release=admit)
            receipt = manager.wait(claim.attempt_id)
            if receipt['state'] != 'EXITED' or receipt.get('returncode') != 0:
                raise ContractError('CPU fixture did not complete successfully')
            if 'cpu-fixture-checksum=333283335000' not in manager.logs(claim.attempt_id):
                raise ContractError('CPU output checksum missing or incorrect')
        except BaseException:
            try:
                stopped = manager.cancel(claim.attempt_id)
                if stopped['state'] in ('EXITED', 'CANCELLED', 'TIMED_OUT', 'LAUNCH_FAILED'):
                    ledger.finish_attempt(claim.run_id, claim.fence, now=time.time(),
                                          elapsed_seconds=time.monotonic() - started,
                                          outcome='failed', process_gone=True)
            except (ValueError, RuntimeError, OSError):
                # Keep claim and reservation if cleanup/readback cannot be proved.
                pass
            raise
        elapsed = time.monotonic() - started
        cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_seconds = (cpu_after.ru_utime + cpu_after.ru_stime
                       - cpu_before.ru_utime - cpu_before.ru_stime)
        ledger.finish_attempt(claim.run_id, claim.fence, now=time.time(),
                              elapsed_seconds=elapsed, outcome='success', process_gone=True)
        # These deliberately tiny rates are synthetic test data, not CPU/GPU
        # compression measurements. The separate subprocess checksum above is real.
        binding = EvidenceBinding(plan.repo, plan.issue, plan.request_id, claim.run_id,
                                  plan.revision, plan_digest, frozen['source_sha'],
                                  plan.source_sha, 'mock-policy-v1')
        artifact = {'schema_version': 1, 'fixture_only': True, 'source_commit': binding.head_sha,
                    'input_protocol': 'detector_input_v1', 'rows': [
                        {'image_key': 'sample-1', 'status': 'complete', 'height': 10, 'width': 10,
                         'latent_bits': 80, 'synthesis_bits': 10, 'entropy_bits': 10, 'total_bits': 100}],
                    'outcome': 'negative', 'cpu_seconds': cpu_seconds, 'reported_mean_bpp': 1.0}
        (root / 'mock_measurements.json').write_bytes(canonical_bytes(artifact))
        snapshot = freeze_artifacts(root, ['mock_measurements.json'],
                                    max_total_bytes=plan.budget.max_output_bytes)
        evidence = MockArtifactValidator().validate(snapshot, binding, ('sample-1',))
        _journal(root / 'validation.json', evidence.to_dict())
        ledger.transition(claim.run_id, claim.fence, 'S10', 'valid_result', now=time.time())
        policy = MergePolicy('mock-policy-v1', ('cpu-tests',))
        pr = PRSnapshot(1, binding, 'fixture-result', 'main', plan.allowed_paths,
                        (CheckResult('cpu-tests', binding.head_sha, 'success'),),
                        mergeable=True, protections_satisfied=True)
        authorization = MergeAuthorization(binding, 1, pr.branch, plan.allowed_paths,
                                           snapshot.manifest_digest, True, True, True, True, True)
        transport = FakeGitHubTransport(pr)
        publisher = FakePublisher(transport, persist_state=lambda state:
                                  _journal(root / 'publication.json', state.to_dict()))
        ledger.transition(claim.run_id, claim.fence, 'S15', 'publish_verified', now=time.time())
        publication = publisher.publish(authorization, evidence, policy)
        if publication.phase != 'MERGED':
            raise ContractError('fake publication did not complete')
        ledger.transition(claim.run_id, claim.fence, 'S14', 'merge_verified', now=time.time())
        ledger.transition(claim.run_id, claim.fence, 'S15', 'close_verified', now=time.time())
        public = render_public_result(evidence)
        bridge.send('report-event', plan.request_id, 'report', public)
        if bridge.readback('report-event')[1] != public:
            raise ContractError('fake report readback mismatch')
        ledger.transition(claim.run_id, claim.fence, 'S15', 'report_verified', now=time.time())
        final = ledger.get_run(claim.run_id)
        return {'mode': 'fixture-only', 'state': final['state'],
                'science_status': final['science_status'], 'report_status': final['report_status'],
                'process_state': receipt['state'], 'checksum_verified': True,
                'fake_merge_calls': transport.merge_calls, 'public_result': public,
                'live_execution': 'BLOCKED_ENV'}
