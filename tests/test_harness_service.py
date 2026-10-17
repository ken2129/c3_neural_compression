"""Offline integrated controller tests; fixtures never grant live authorization."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_harness.intake_flow_fixture import FIXTURE_NOW, FixtureSource
from research_harness.issue_intake import MARKER, PREFIX, content_hash, timestamp
from research_harness.result_publisher import (EXPECTED_CHECKSUM, SCHEMA,
    GitHubResultPublisher, ValidatedCanaryResult)
from research_harness.schema import ContractError, canonical
from research_harness.service import Controller
from research_harness.service_state import ServiceState, initialize
from test_harness_result_publisher import FakeTransport, pr_fixture

SOURCE_SHA = 'b' * 40
ISSUE = 70


class ServiceSource(FixtureSource):
    """Adapt the existing synthetic owner fixture; never read a real Issue."""
    def __init__(self, number=ISSUE):
        super().__init__()
        self.issue['number'] = number
        self.request = json.loads(self.issue['body'].split(MARKER)[1].split('\n```')[0])
        self.request['plan'] = dict(kind='cpu-canary-v1', source_sha=SOURCE_SHA, publish_result_pr=True)
        self.extra_comments = []
        self.approval_present = True
        self.candidates = [number]
        self.reads = 0
        self.list_calls = 0
        self.list_error = None
        self.render(refresh_receipt=True)

    def render(self, *, refresh_receipt=False):
        self.issue['body'] = 'Offline integration fixture.' + MARKER + json.dumps(self.request) + '\n```'
        if refresh_receipt:
            body = json.loads(self.comment['body'])
            body.update(issue=self.issue['number'], content_sha256=content_hash(self.issue),
                        expires_at=self.request['expires_at'])
            self.comment['body'] = json.dumps(body)

    def get_issue(self, number):
        if number != self.issue['number']:
            raise ContractError('unknown offline Issue')
        self.reads += 1
        return copy.deepcopy(self.issue)

    def comments(self, number):
        if number != self.issue['number']:
            raise ContractError('unknown offline Issue')
        return copy.deepcopy(([self.comment] if self.approval_present else []) + self.extra_comments)

    def list_candidates(self):
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return {number: copy.deepcopy(self.issue) for number in self.candidates}

    def revoke(self):
        body = json.loads(self.comment['body'])
        body['decision'] = 'cancel-execution'
        self.extra_comments.append(dict(copy.deepcopy(self.comment), id='IC_SYNTHETIC_CANCEL', body=json.dumps(body)))


class FakeRuntime:
    """Models the trusted runtime interface without processes, credentials or models."""
    def __init__(self):
        self.preflights = 0
        self.runs = 0
        self.on_preflight = lambda: None
        self.on_run = lambda: None
        self.on_result = lambda: None
        self.preflight_error = None
        self.run_error = None
        self.result_transform = lambda result: result
        self.outcome = 'success'
        self.seen_bindings = []

    def preflight(self):
        self.preflights += 1
        self.on_preflight()
        if self.preflight_error is not None:
            raise self.preflight_error

    def run(self, binding, recheck):
        self.runs += 1
        self.seen_bindings.append(copy.deepcopy(binding))
        self.on_run()
        recheck()
        if self.run_error is not None:
            raise self.run_error
        expected = {key: binding[key] for key in ('request_id', 'run_id', 'issue_number', 'plan_digest', 'source_sha')}
        data = dict(schema=SCHEMA, **expected, outcome=self.outcome,
                    checksum=EXPECTED_CHECKSUM if self.outcome == 'success' else None, elapsed_seconds=.01)
        result = ValidatedCanaryResult.from_bytes(canonical(data), **expected)
        self.on_result()
        return self.result_transform(result)


class PublisherFactory:
    """Use the production publication state machine over existing fake GitHub I/O."""
    def __init__(self):
        self.transport = None
        self.calls = 0
        self.requests = []
        self.modes = {}
        self.error = None
        self.before_publish = lambda request: None
        self.configure_transport = lambda transport: None

    def __call__(self, persist):
        factory = self
        class Publisher:
            def publish(self, request, *, state=None, authorized=False, authorize_callback=None):
                factory.calls += 1
                factory.requests.append(request)
                factory.before_publish(request)
                if factory.error is not None:
                    raise factory.error
                if factory.transport is None:
                    factory.transport = FakeTransport(request)
                    factory.transport.modes.update(factory.modes)
                    factory.configure_transport(factory.transport)
                return GitHubResultPublisher(factory.transport, persist_state=persist).publish(
                    request, state, authorized=authorized, authorize_callback=authorize_callback)
        return Publisher()


class ControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        initialize(self.root)
        self.state = ServiceState(self.root)
        self.addCleanup(lambda: self.state.close())
        self.source = ServiceSource()
        self.runtime = FakeRuntime()
        self.publishers = PublisherFactory()
        self.now = FIXTURE_NOW
        self.make_controller()

    def make_controller(self):
        self.controller = Controller(self.state, self.source, self.source, self.runtime, self.publishers,
                                     source_sha=SOURCE_SHA, clock=lambda: self.now)

    def restart(self):
        self.state.close()
        self.state = ServiceState(self.root)
        self.make_controller()

    def assert_no_dispatch(self):
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.publishers.calls, 0)
        self.assertIsNone(self.state.get(self.source.issue['number']))
        self.assertEqual(list((self.root / 'claims').iterdir()), [])

    def test_success_saves_validated_result_before_pr_and_keeps_notification_separate(self):
        def before_publish(request):
            saved = self.state.get(ISSUE)
            self.assertEqual(saved['phase'], 'RESULT_READY')
            self.assertEqual(saved['result'], request.to_dict())
            self.assertEqual(saved['binding'], self.runtime.seen_bindings[0])
        self.publishers.before_publish = before_publish
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertEqual(report['publication_phase'], 'PUBLISHED')
        self.assertIsNone(report['publication_reason'])
        self.assertTrue(report['dispatched'])
        self.assertFalse(report['duplicate'])
        self.assertEqual(report['notification'], 'UNCONFIGURED')
        self.assertEqual(report['pr_url'], 'https://github.com/ken2129/c3_neural_compression/pull/123')
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.runtime.preflights, 1)
        self.assertEqual(self.publishers.transport.events, ['branch', 'content', 'pr'])
        saved = self.state.get(ISSUE)
        self.assertEqual(saved['publication']['phase'], 'PUBLISHED')
        self.assertEqual(saved['result']['result']['artifact_sha256'], self.publishers.requests[0].result.artifact_sha256)

    def test_completed_run_restart_has_no_duplicate_science_or_publication(self):
        self.controller.process(ISSUE)
        self.restart()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertFalse(report['dispatched'])
        self.assertTrue(report['duplicate'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.runtime.preflights, 1)
        self.assertEqual(self.publishers.calls, 1)
        self.assertEqual(self.publishers.transport.events, ['branch', 'content', 'pr'])

    def test_completed_publication_remains_historical_after_approval_expires(self):
        original = self.controller.process(ISSUE)
        self.now = timestamp(self.source.request['expires_at']) + 1
        self.restart()
        with patch.object(self.source, 'get_issue', side_effect=AssertionError('historical result needs no new approval')):
            report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertIs(report['historical'], True)
        self.assertFalse(report['dispatched'])
        self.assertTrue(report['duplicate'])
        self.assertEqual(report['pr_url'], original['pr_url'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 1)

    def test_completed_publication_is_not_rewritten_by_later_cancel_or_issue_change(self):
        original = self.controller.process(ISSUE)
        self.source.revoke()
        self.source.issue['title'] = 'Changed after verified publication'
        self.restart()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertIs(report['historical'], True)
        self.assertEqual(report['pr_url'], original['pr_url'])
        self.assertEqual(self.publishers.transport.events, ['branch', 'content', 'pr'])
        self.assertEqual(self.runtime.runs, 1)

    def test_claimed_restart_does_not_guess_that_a_process_never_started(self):
        binding = self.controller.approve(ISSUE)
        self.assertTrue(self.state.claim(binding))
        self.restart()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'CLAIMED')
        self.assertTrue(report['duplicate'])
        self.assertFalse(report['dispatched'])
        self.assertEqual(self.runtime.preflights, 0)
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.publishers.calls, 0)

    def test_blocked_restart_never_retries_a_failed_or_unknown_run(self):
        self.runtime.run_error = RuntimeError('synthetic runtime failure')
        with self.assertRaises(RuntimeError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.runtime.run_error = None
        self.restart()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'BLOCKED')
        self.assertTrue(report['duplicate'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_runtime_setup_failure_precedes_claim_and_allows_setup_retry(self):
        self.runtime.preflight_error = ContractError('normal sandbox and isolation not verified')
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_no_dispatch()
        self.runtime.preflight_error = None
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertEqual(self.runtime.runs, 1)

    def test_explicit_cancel_before_admission_never_preflights_or_claims(self):
        self.source.revoke()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_no_dispatch()
        self.assertEqual(self.runtime.preflights, 0)

    def test_expiry_before_admission_never_preflights_or_claims(self):
        self.now = timestamp(self.source.request['expires_at'])
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_no_dispatch()
        self.assertEqual(self.runtime.preflights, 0)

    def test_change_after_preflight_is_rejected_before_claim(self):
        self.runtime.on_preflight = lambda: self.source.issue.update(title=PREFIX + ' modified')
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_no_dispatch()
        self.assertEqual(self.runtime.preflights, 1)

    def test_cancel_after_claim_blocks_without_starting_runtime(self):
        original = self.state.claim
        def claim(binding, *, epoch=None):
            value = original(binding, epoch=epoch)
            self.source.revoke()
            return value
        with patch.object(self.state, 'claim', side_effect=claim), self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(self.publishers.calls, 0)

    def test_runtime_recheck_detects_cancellation_and_expiry(self):
        for condition in ('cancel', 'expire'):
            with self.subTest(condition=condition):
                # Each condition owns a fresh durable state and unmodified receipt.
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder); initialize(root)
                    state = ServiceState(root)
                    try:
                        source, runtime, factory = ServiceSource(), FakeRuntime(), PublisherFactory()
                        now = [FIXTURE_NOW]
                        controller = Controller(state, source, source, runtime, factory,
                                                source_sha=SOURCE_SHA, clock=lambda: now[0])
                        runtime.on_run = source.revoke if condition == 'cancel' else lambda: now.__setitem__(0, timestamp(source.request['expires_at']))
                        with self.assertRaises(ContractError):
                            controller.process(ISSUE)
                        self.assertEqual(runtime.runs, 1)
                        self.assertEqual(state.get(ISSUE)['phase'], 'BLOCKED')
                        self.assertIsNone(state.get(ISSUE)['result'])
                        self.assertEqual(factory.calls, 0)
                    finally:
                        state.close()

    def test_post_runtime_approval_recheck_prevents_result_or_publication(self):
        self.runtime.on_result = self.source.revoke
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertIsNone(self.state.get(ISSUE)['result'])
        self.assertEqual(self.publishers.calls, 0)

    def test_post_runtime_expiry_prevents_saving_or_publishing_result(self):
        def expire():
            self.now = timestamp(self.source.request['expires_at'])
        self.runtime.on_result = expire
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertIsNone(self.state.get(ISSUE)['result'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_post_runtime_issue_change_prevents_publication(self):
        self.runtime.on_result = lambda: self.source.issue.update(title=PREFIX + ' replaced after run')
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_cancel_after_result_saved_blocks_publication_and_never_recomputes(self):
        original = self.state.save_result
        def save(number, result, *, await_restart=False):
            original(number, result, await_restart=await_restart)
            self.source.revoke()
        with patch.object(self.state, 'save_result', side_effect=save), self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'RESULT_READY')
        self.assertEqual(self.publishers.calls, 0)
        self.restart()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_unknown_pr_restart_reconciles_existing_result_without_computing(self):
        self.publishers.modes['pr'] = 'unknown_before'
        first = self.controller.process(ISSUE)
        self.assertEqual(first['phase'], 'RESULT_READY')
        self.assertEqual(first['publication_phase'], 'PR_UNKNOWN')
        self.assertIsNone(first['publication_reason'])
        self.assertEqual(self.state.get(ISSUE)['publication']['phase'], 'PR_UNKNOWN')
        self.assertEqual(self.runtime.runs, 1)
        self.restart()
        unresolved = self.controller.process(ISSUE)
        self.assertEqual(unresolved['phase'], 'RESULT_READY')
        self.assertFalse(unresolved['dispatched'])
        transport = self.publishers.transport
        self.assertEqual(transport.events.count('pr'), 1)
        transport.pr = pr_fixture(transport.request)
        self.restart()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertFalse(report['dispatched'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.runtime.preflights, 1)
        self.assertEqual(transport.events, ['branch', 'content', 'pr'])

    def test_cancelled_unknown_pr_restart_performs_only_readback(self):
        self.publishers.modes['pr'] = 'unknown_before'
        self.controller.process(ISSUE)
        transport = self.publishers.transport
        self.source.revoke()
        self.restart()
        pending = self.controller.process(ISSUE)
        self.assertEqual(pending['publication_phase'], 'PR_UNKNOWN')
        self.assertFalse(pending['dispatched'])
        self.assertEqual(transport.events, ['branch', 'content', 'pr'])
        transport.pr = pr_fixture(transport.request)
        self.restart()
        recovered = self.controller.process(ISSUE)
        self.assertEqual(recovered['phase'], 'PUBLISHED')
        self.assertEqual(recovered['publication_phase'], 'PUBLISHED')
        self.assertFalse(recovered['dispatched'])
        self.assertEqual(transport.events, ['branch', 'content', 'pr'])
        self.assertEqual(self.runtime.runs, 1)

    def test_cancelled_unknown_branch_can_resolve_but_never_write_content(self):
        self.publishers.modes['branch'] = 'unknown_before'
        self.controller.process(ISSUE)
        transport = self.publishers.transport
        self.source.revoke()
        self.restart()
        pending = self.controller.process(ISSUE)
        self.assertEqual(pending['publication_phase'], 'BRANCH_UNKNOWN')
        transport.head = transport.request.base_sha
        self.restart()
        recovered = self.controller.process(ISSUE)
        self.assertEqual(recovered['publication_phase'], 'BLOCKED')
        self.assertEqual(recovered['publication_reason'], 'AUTHORIZATION_MISSING')
        self.assertEqual(transport.events, ['branch'])
        self.assertEqual(self.runtime.runs, 1)

    def test_expired_unknown_content_can_resolve_but_never_create_pr(self):
        self.publishers.modes['content'] = 'unknown_before'
        self.controller.process(ISSUE)
        transport = self.publishers.transport
        self.now = timestamp(self.source.request['expires_at'])
        self.restart()
        pending = self.controller.process(ISSUE)
        self.assertEqual(pending['publication_phase'], 'CONTENT_UNKNOWN')
        transport.head = 'e' * 40
        transport.content = transport.request.content
        self.restart()
        recovered = self.controller.process(ISSUE)
        self.assertEqual(recovered['publication_phase'], 'BLOCKED')
        self.assertEqual(recovered['publication_reason'], 'AUTHORIZATION_MISSING')
        self.assertEqual(transport.events, ['branch', 'content'])
        self.assertEqual(self.runtime.runs, 1)

    def test_cancellation_between_branch_and_content_stops_publication_writes(self):
        def configure(transport):
            original = transport.create_branch
            def create(request):
                original(request)
                self.source.revoke()
            transport.create_branch = create
        self.publishers.configure_transport = configure
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'RESULT_READY')
        self.assertEqual(self.state.get(ISSUE)['publication']['phase'], 'BLOCKED')
        self.assertEqual(self.state.get(ISSUE)['publication']['reason'], 'AUTHORIZATION_MISSING')
        self.assertEqual(report['publication_phase'], 'BLOCKED')
        self.assertEqual(report['publication_reason'], 'AUTHORIZATION_MISSING')
        self.assertEqual(self.publishers.transport.events, ['branch'])
        self.assertEqual(self.runtime.runs, 1)

    def test_expiry_between_content_and_pr_creation_stops_new_pr(self):
        def configure(transport):
            original = transport.create_content
            def create(request):
                original(request)
                self.now = timestamp(self.source.request['expires_at'])
            transport.create_content = create
        self.publishers.configure_transport = configure
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'RESULT_READY')
        self.assertEqual(self.state.get(ISSUE)['publication']['phase'], 'BLOCKED')
        self.assertEqual(self.state.get(ISSUE)['publication']['reason'], 'AUTHORIZATION_MISSING')
        self.assertEqual(report['publication_phase'], 'BLOCKED')
        self.assertEqual(report['publication_reason'], 'AUTHORIZATION_MISSING')
        self.assertEqual(self.publishers.transport.events, ['branch', 'content'])
        self.assertEqual(self.runtime.runs, 1)

    def test_publisher_setup_failure_leaves_saved_result_for_restart_only(self):
        self.publishers.error = ContractError('publication transport remains disabled')
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'RESULT_READY')
        self.publishers.error = None
        self.restart()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertFalse(report['dispatched'])
        self.assertEqual(self.runtime.runs, 1)

    def test_runtime_output_type_and_binding_are_independently_checked(self):
        for transform in (lambda result: {'validated': True}, lambda result: replace(result, run_id='other-run')):
            with self.subTest(transform=transform):
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder); initialize(root)
                    state = ServiceState(root)
                    try:
                        source, runtime, factory = ServiceSource(), FakeRuntime(), PublisherFactory()
                        runtime.result_transform = transform
                        controller = Controller(state, source, source, runtime, factory,
                                                source_sha=SOURCE_SHA, clock=lambda: FIXTURE_NOW)
                        with self.assertRaises(ContractError):
                            controller.process(ISSUE)
                        self.assertEqual(state.get(ISSUE)['phase'], 'BLOCKED')
                        self.assertIsNone(state.get(ISSUE)['result'])
                        self.assertEqual(factory.calls, 0)
                    finally:
                        state.close()

    def test_harness_issue_56_is_excluded_even_with_a_matching_owner_receipt(self):
        self.source = ServiceSource(56)
        self.make_controller()
        with self.assertRaises(ContractError):
            self.controller.process(56)
        self.assert_no_dispatch()
        self.assertEqual(self.runtime.preflights, 0)

    def test_title_prefix_alone_and_missing_owner_approval_are_not_authority(self):
        self.source.approval_present = False
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_no_dispatch()
        self.source.approval_present = True
        self.source.issue['body'] = 'Please run this because the title says approved.'
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_no_dispatch()

    def test_ordinary_issue_without_prefix_never_dispatches(self):
        self.source.issue['title'] = 'Ordinary research design discussion'
        self.source.render(refresh_receipt=True)
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_no_dispatch()
        self.assertEqual(self.runtime.preflights, 0)

    def test_fixed_plan_rejects_commands_repositories_foreign_source_and_boolean_flags(self):
        changes = ({'command': 'curl https://evil.test'}, {'repository': 'other/repo'},
                   {'kind': 'gpu-training'}, {'source_sha': 'f' * 40},
                   {'publish_result_pr': 1}, {'publish_result_pr': False},
                   {'runtime_verified': True})
        for changeset in changes:
            with self.subTest(changes=changeset):
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder); initialize(root)
                    state = ServiceState(root)
                    try:
                        source, runtime, factory = ServiceSource(), FakeRuntime(), PublisherFactory()
                        source.request['plan'].update(changeset)
                        source.render(refresh_receipt=True)
                        controller = Controller(state, source, source, runtime, factory,
                                                source_sha=SOURCE_SHA, clock=lambda: FIXTURE_NOW)
                        with self.assertRaises(ContractError):
                            controller.process(ISSUE)
                        self.assertIsNone(state.get(ISSUE))
                        self.assertEqual(runtime.preflights, 0)
                        self.assertEqual(runtime.runs, 0)
                        self.assertEqual(factory.calls, 0)
                    finally:
                        state.close()

    def test_listing_failure_aborts_before_any_dispatch(self):
        self.source.list_error = RuntimeError('offline incomplete listing')
        with self.assertRaises(RuntimeError):
            self.controller.once()
        self.assert_no_dispatch()
        self.assertEqual(self.source.reads, 0)

    def test_once_failure_after_durable_claim_does_not_report_definitely_unstarted(self):
        self.runtime.run_error = RuntimeError('synthetic execution outcome unknown')
        reports = self.controller.once()
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(reports[0]['dispatched'], 'unknown')
        self.assertEqual(reports[0]['phase'], 'BLOCKED')
        self.assertEqual(self.publishers.calls, 0)

    def test_once_failure_without_claim_reports_no_dispatch(self):
        self.runtime.preflight_error = ContractError('runtime profile not verified')
        reports = self.controller.once()
        self.assertIs(reports[0]['dispatched'], False)
        self.assertEqual(reports[0]['phase'], 'BLOCKED_OR_UNCONFIGURED')
        self.assert_no_dispatch()

    def test_poll_listing_failure_emits_unavailable_and_never_dispatches(self):
        self.source.list_error = RuntimeError('offline listing failed')
        output = []
        self.controller.poll(stop=lambda: bool(output), emit=output.append, interval=30,
                             sleep=lambda _: self.fail('completed poll should not sleep'))
        self.assertEqual(output, [[{'phase': 'SOURCE_UNAVAILABLE', 'dispatched': False, 'error_category': 'OTHER'}]])
        self.assert_no_dispatch()

    def test_once_rereads_tracked_issue_when_prefix_removed_from_listing(self):
        self.controller.approve(ISSUE)
        self.source.issue['title'] = 'Removed approval marker'
        self.source.candidates = []
        reports = self.controller.once()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['issue'], ISSUE)
        self.assertEqual(reports[0]['phase'], 'BLOCKED_OR_UNCONFIGURED')
        self.assert_no_dispatch()

    def test_poll_interval_is_bounded(self):
        for interval in (True, 0, 29, 3601, 30.5):
            with self.assertRaises(ContractError):
                self.controller.poll(stop=lambda: True, interval=interval)
        self.controller.poll(stop=lambda: True, interval=30)
        self.assert_no_dispatch()


if __name__ == '__main__':
    unittest.main()
