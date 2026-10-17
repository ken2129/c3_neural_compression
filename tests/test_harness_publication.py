"""No GitHub network calls: merge guard, compare-and-swap and recovery fixtures."""
from dataclasses import replace
import unittest

from research_harness.artifacts import ContractError
from research_harness.publication import (CheckResult, FakeGitHubTransport, FakePublisher,
    MergeAuthorization, MergeGate, MergePolicy, PRSnapshot, PublicationState)
from test_harness_artifacts import fixture_evidence


def publication_fixture():
    snapshot, evidence = fixture_evidence()
    binding = evidence.binding
    policy = MergePolicy("mock-policy-v1", ("cpu-tests",))
    pr = PRSnapshot(1, binding, "result-run-1", "main", ("docs/research/mock-result.md",),
                    (CheckResult("cpu-tests", binding.head_sha, "success"),),
                    mergeable=True, protections_satisfied=True)
    authorization = MergeAuthorization(binding, pr.number, pr.branch, pr.changed_paths,
                                        snapshot.manifest_digest, True, True, True, True, True)
    return pr, authorization, evidence, policy


class PublicationTests(unittest.TestCase):
    def test_valid_negative_result_merges_once_and_confirms_issue_close(self):
        pr, auth, evidence, policy = publication_fixture()
        transport = FakeGitHubTransport(pr)
        publisher = FakePublisher(transport)
        self.assertTrue(MergeGate.evaluate(pr, auth, evidence, policy).allowed)
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "MERGED")
        self.assertIsNotNone(publisher.state.merge_commit)
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "MERGED")
        self.assertEqual(transport.merge_calls, 1)

    def test_each_missing_authority_or_completion_blocks(self):
        pr, auth, evidence, policy = publication_fixture()
        for field in ("execution_approved", "result_publication_approved", "result_merge_approved", "approval_current", "complete_result"):
            transport = FakeGitHubTransport(pr)
            publisher = FakePublisher(transport)
            self.assertEqual(publisher.publish(replace(auth, **{field: False}), evidence, policy).phase, "BLOCKED")
            self.assertEqual(transport.merge_calls, 0)

    def test_head_base_identity_artifact_and_version_changes_invalidate(self):
        pr, auth, evidence, policy = publication_fixture()
        bindings = [replace(pr.binding, **change) for change in (
            {"head_sha": "d" * 40}, {"base_sha": "e" * 40}, {"request_id": "request-2"},
            {"run_id": "run-2"}, {"issue_number": 900002}, {"plan_revision": 2},
            {"plan_digest": "f" * 64}, {"policy_version": "policy-v2"})]
        for binding in bindings:
            self.assertFalse(MergeGate.evaluate(replace(pr, binding=binding), auth, evidence, policy).allowed)
        self.assertFalse(MergeGate.evaluate(pr, replace(auth, artifact_digest="f" * 64), evidence, policy).allowed)
        self.assertFalse(MergeGate.evaluate(pr, auth, evidence, replace(policy, policy_version="policy-v2")).allowed)
        self.assertFalse(MergeGate.evaluate(replace(pr, number=2), auth, evidence, policy).allowed)
        self.assertFalse(MergeGate.evaluate(replace(pr, branch="different"), auth, evidence, policy).allowed)

    def test_harness_policy_ci_auth_environment_and_unknown_scope_excluded(self):
        pr, auth, evidence, policy = publication_fixture()
        for path in ("research_harness/controller.py", ".github/workflows/test.yml", "docs/experiment_policy.md",
                     "tests/test_harness_publication.py", "experiments/auth.py", "experiments/credentials.py",
                     "configs/environment.yml", "requirements.txt", "Dockerfile", "evaluation/comparison.py",
                     "README.md", "docs/research/unapproved.md"):
            # Even explicitly allowlisting a protected path cannot relax exclusions.
            candidate_auth = replace(auth, allowed_paths=auth.allowed_paths + (path,)) if path != "docs/research/unapproved.md" else auth
            self.assertFalse(MergeGate.evaluate(replace(pr, changed_paths=(path,)), candidate_auth, evidence, policy).allowed, path)
        binding = replace(pr.binding, issue_number=56)
        self.assertFalse(MergeGate.evaluate(replace(pr, binding=binding), replace(auth, binding=binding),
                                           replace(evidence, binding=binding), policy).allowed)

    def test_required_checks_never_vacuously_pass(self):
        pr, auth, evidence, policy = publication_fixture()
        with self.assertRaises(ContractError):
            MergePolicy("policy", ())
        for checks in ((), (CheckResult("other-test", pr.binding.head_sha, "success"),),
                       (CheckResult("cpu-tests", "d" * 40, "success"),)):
            self.assertFalse(MergeGate.evaluate(replace(pr, checks=checks), auth, evidence, policy).allowed)
        for conclusion in ("failure", "pending", "unknown", "cancelled", "skipped"):
            checks = (CheckResult("cpu-tests", pr.binding.head_sha, conclusion),)
            self.assertFalse(MergeGate.evaluate(replace(pr, checks=checks), auth, evidence, policy).allowed)
        with self.assertRaises(ContractError):
            replace(pr, checks=pr.checks * 2)

    def test_unknown_protection_draft_closed_and_wrong_base_block(self):
        pr, auth, evidence, policy = publication_fixture()
        for changes in ({"mergeable": None}, {"mergeable": False}, {"protections_satisfied": None},
                        {"protections_satisfied": False}, {"draft": True}, {"state": "closed"}, {"base_branch": "other"}):
            self.assertFalse(MergeGate.evaluate(replace(pr, **changes), auth, evidence, policy).allowed)

    def test_unknown_after_merge_restores_and_reads_back_without_resend(self):
        pr, auth, evidence, policy = publication_fixture()
        transport = FakeGitHubTransport(pr)
        transport.next_merge_response = "unknown_after"
        saved = []
        publisher = FakePublisher(transport, persist_state=saved.append)
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "MERGE_UNKNOWN")
        self.assertEqual(saved[0].phase, "MERGE_UNKNOWN")
        restored = FakePublisher(transport, PublicationState.from_dict(saved[-1].to_dict()))
        self.assertEqual(restored.publish(auth, evidence, policy).phase, "MERGED")
        self.assertEqual(transport.merge_calls, 1)

    def test_intent_journal_failure_prevents_merge(self):
        pr, auth, evidence, policy = publication_fixture()
        transport = FakeGitHubTransport(pr)
        def fail(_):
            raise OSError("journal unavailable")
        publisher = FakePublisher(transport, persist_state=fail)
        with self.assertRaises(OSError):
            publisher.publish(auth, evidence, policy)
        self.assertEqual(transport.merge_calls, 0)

    def test_checks_changed_after_intent_are_rechecked_before_merge(self):
        pr, auth, evidence, policy = publication_fixture()
        transport = FakeGitHubTransport(pr)
        def change_checks(state):
            if state.phase == "MERGE_UNKNOWN":
                transport.pr = replace(pr, checks=())
        publisher = FakePublisher(transport, persist_state=change_checks)
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "BLOCKED")
        self.assertEqual(transport.merge_calls, 0)

    def test_open_readback_alone_is_not_nonapplication_proof(self):
        pr, auth, evidence, policy = publication_fixture()
        transport = FakeGitHubTransport(pr)
        transport.next_merge_response = "unknown_before"
        publisher = FakePublisher(transport)
        publisher.publish(auth, evidence, policy)
        transport.definitively_not_applied = False
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "MERGE_UNKNOWN")
        transport.available = False
        self.assertEqual(publisher.reconcile().phase, "MERGE_UNKNOWN")
        self.assertEqual(transport.merge_calls, 1)
        transport.available = True
        transport.definitively_not_applied = True
        self.assertEqual(publisher.reconcile().phase, "RETRY_READY")
        transport.pr = replace(pr, checks=())
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "BLOCKED")
        self.assertEqual(transport.merge_calls, 1)
        transport.pr = pr
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "MERGED")
        self.assertEqual(transport.merge_calls, 2)

    def test_close_pending_does_not_retry_science_merge_or_close_issue(self):
        pr, auth, evidence, policy = publication_fixture()
        transport = FakeGitHubTransport(pr)
        transport.close_on_merge = False
        publisher = FakePublisher(transport)
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "CLOSE_PENDING")
        self.assertEqual(publisher.publish(auth, evidence, policy).phase, "CLOSE_PENDING")
        transport.pr = replace(transport.pr, issue_closed=True)
        self.assertEqual(publisher.reconcile().phase, "MERGED")
        self.assertEqual(transport.merge_calls, 1)

    def test_unexpected_external_merge_requires_review(self):
        pr, auth, evidence, policy = publication_fixture()
        transport = FakeGitHubTransport(pr)
        transport.next_merge_response = "unknown_after"
        publisher = FakePublisher(transport)
        publisher.publish(auth, evidence, policy)
        transport.pr = replace(transport.pr, merged_head_sha="e" * 40)
        self.assertEqual(publisher.reconcile().phase, "EXTERNAL_REVIEW")
        publisher.publish(auth, evidence, policy)
        self.assertEqual(transport.merge_calls, 1)

    def test_transport_compare_and_swap_rejects_race(self):
        pr, _, _, _ = publication_fixture()
        transport = FakeGitHubTransport(replace(pr, binding=replace(pr.binding, head_sha="e" * 40)))
        self.assertEqual(transport.merge(expected_head=pr.binding.head_sha, expected_base=pr.binding.base_sha), "rejected")
        self.assertEqual(transport.pr.state, "open")

    def test_strict_schemas_and_real_transport_unsupported(self):
        pr, auth, _, policy = publication_fixture()
        for record in (pr, auth, policy, PublicationState()):
            self.assertEqual(type(record).from_dict(record.to_dict()), record)
            with self.assertRaises(ContractError):
                type(record).from_dict({**record.to_dict(), "bypass": True})
        with self.assertRaises(ContractError):
            FakePublisher(object())
        with self.assertRaises(ContractError):
            replace(policy, fixture_only=False)


if __name__ == "__main__":
    unittest.main()
