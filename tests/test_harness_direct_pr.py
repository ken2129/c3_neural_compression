"""Offline direct-PR contracts and dispatch: no models, GitHub I/O or deployment."""
from copy import deepcopy
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import hashlib
import io
import os
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_harness.direct_pr_service import DirectPRController, DirectPRResult
from research_harness.goal_plan import (DIRECT_PR_KIND, GOAL_RESOURCE_BUDGET,
    direct_pr_scope, require_installed_goal, validate_goal_plan)
from research_harness.goal_runner import (DIRECT_PR_RESPONSE_SCHEMA,
    GOAL_RESPONSE_SCHEMA, GoalAppServerRunner, GoalCandidate, GoalRequest, goal_prompt)
import research_harness.goal_runtime as runtime
import research_harness.direct_pr_runtime as direct_runtime
import research_harness.model_deployment as model_deployment
from research_harness.intake_flow_fixture import FIXTURE_NOW
from research_harness.issue_intake import timestamp
from research_harness.schema import ContractError, REPOSITORY, canonical
from research_harness.service_state import ServiceState, initialize
from research_harness.worker_pr import (CONSENTS, OWNER_ID, OWNER_LOGIN,
    default_worker_pr_config, validate_worker_pr_config)
from test_harness_goal_runner import Transport, frames, plan
from test_harness_goal_runtime import GoalOS
from test_harness_restart_boundary import epoch
from test_harness_service import ISSUE, SOURCE_SHA, ServiceSource
from test_harness_service_restart import EpochRuntime
from tests.test_harness_uid_supervisor import NamespaceExit
from tests.test_harness_goal_deployment import launcher, prepare


PR_URL = f'https://github.com/{REPOSITORY}/pull/123'
LIMITS = dict(max_seconds=120, **GOAL_RESOURCE_BUDGET)


def direct_plan(number=ISSUE):
    value = plan()
    value.update(kind=DIRECT_PR_KIND, source_sha=SOURCE_SHA,
        direct_pr=dict(repository=REPOSITORY, issue_number=number,
            base_branch='main', base_sha=SOURCE_SHA,
            title=f'[Codex] Research for Issue {number}', public_results_approved=True))
    return value


def enabled_config():
    return dict(default_worker_pr_config(), enabled=True,
        actor_login='offline-worker-bot', actor_id=123456789,
        **dict.fromkeys(CONSENTS, True))


def direct_source(number=ISSUE):
    source = ServiceSource(number)
    source.request['plan'] = direct_plan(number)
    # Each synthetic approval is a different immutable comment, as on GitHub.
    source.comment['id'] = f'IC_OFFLINE_DIRECT_{number}'
    source.render(refresh_receipt=True)
    return source


class DirectRuntime(EpochRuntime):
    """Returns only an untrusted completion report, never a verified PR."""
    def prepare_plan(self, value):
        self.plan = deepcopy(value)

    def run(self, binding, recheck):
        self.runs += 1
        self.seen_bindings.append(deepcopy(binding))
        self.on_run()
        recheck()
        if self.run_error is not None:
            raise self.run_error
        result = DirectPRResult(deepcopy(binding), self.plan['model'],
            'completed', 'Worker-reported result only.', PR_URL, .5)
        self.on_result()
        return self.result_transform(result)


class MultipleSources:
    def __init__(self, *sources):
        self.sources = {source.issue['number']: source for source in sources}

    def get_issue(self, number):
        return self.sources[number].get_issue(number)

    def comments(self, number):
        return self.sources[number].comments(number)

    def list_candidates(self):
        return {number: source.get_issue(number) for number, source in self.sources.items()}


class DirectPlanTests(unittest.TestCase):
    def test_valid_plan_is_detached_and_bound_to_installed_source(self):
        value = direct_plan()
        checked = validate_goal_plan(value, SOURCE_SHA,
            resource_limits=LIMITS, deployment_id='1' * 32)
        self.assertEqual(checked, value)
        checked['direct_pr']['title'] = '[Codex] Changed copy'
        self.assertNotEqual(checked, value)

    def test_publication_target_and_explicit_consent_are_closed(self):
        changes = ({'repository': 'other/repository'}, {'issue_number': True},
            {'issue_number': 0}, {'issue_number': 56}, {'base_branch': 'other'},
            {'base_sha': 'a' * 40}, {'title': 'No Codex prefix'},
            {'title': '[Codex] first\nsecond'}, {'public_results_approved': False},
            {'public_results_approved': 1}, {'extra': 'not approved'})
        for change in changes:
            with self.subTest(change=change):
                value = direct_plan()
                value['direct_pr'].update(change)
                with self.assertRaises(ContractError):
                    validate_goal_plan(value, SOURCE_SHA)
        value = direct_plan()
        del value['direct_pr']
        with self.assertRaises(ContractError):
            validate_goal_plan(value, SOURCE_SHA)

    def test_model_source_resources_and_deployment_still_required(self):
        for change in ({'source_sha': 'f' * 40}, {'deployment_id': '2' * 32},
                       {'model': 'model-latest'}, {'model_usage_approved': False},
                       {'max_seconds': 121}, {'sandbox_mode': 'danger-full-access'}):
            with self.subTest(change=change):
                with self.assertRaises(ContractError):
                    validate_goal_plan(dict(direct_plan(), **change), SOURCE_SHA,
                        resource_limits=LIMITS, deployment_id='1' * 32)

    def test_distinct_goals_fit_same_installed_envelope(self):
        original = direct_plan()
        profile = {'direct_pr_scope': direct_pr_scope(original)}
        another = direct_plan(ISSUE + 1)
        another['goal'] = 'Investigate a different approved question.'
        another['completion_criteria'] = ['Report the independent experiment.']
        self.assertNotEqual(canonical(original), canonical(another))
        require_installed_goal(profile, another)
        for key in ('inputs', 'write_scope'):
            with self.subTest(key=key):
                expanded = deepcopy(another)
                expanded[key].append('uninstalled')
                with self.assertRaises(ContractError):
                    require_installed_goal(profile, expanded)

    def test_disabled_by_default_and_explicit_actor_consents_required(self):
        self.assertIs(validate_worker_pr_config(None)['enabled'], False)
        self.assertIs(validate_worker_pr_config(enabled_config())['enabled'], True)
        for key in ('actor_login', 'actor_id', *CONSENTS):
            with self.subTest(key=key):
                value = enabled_config()
                value[key] = None if key.startswith('actor_') else False
                with self.assertRaises(ContractError):
                    validate_worker_pr_config(value)

    def test_owner_login_or_id_cannot_be_worker_actor(self):
        for change in ({'actor_login': OWNER_LOGIN}, {'actor_login': OWNER_LOGIN.upper()},
                       {'actor_id': OWNER_ID}):
            with self.subTest(change=change):
                with self.assertRaises(ContractError):
                    validate_worker_pr_config(dict(enabled_config(), **change))


class DirectRunnerTests(unittest.TestCase):
    def run_candidate(self, candidate):
        values = frames()
        encoded = json.dumps(candidate)
        values[-2]['params']['item']['text'] = encoded
        values[-1]['params']['turn']['items'][0]['text'] = encoded
        transport = Transport(values)
        result = GoalAppServerRunner(enabled=True).run(
            GoalRequest(direct_plan(), SOURCE_SHA), transport,
            admit=lambda request: None, recheck=lambda: None)
        return result, transport

    def test_prompt_contains_codex_title_target_and_agent_autonomy(self):
        value = direct_plan()
        prompt = goal_prompt(value)
        self.assertIn('[Codex]', prompt)
        self.assertIn('Issue reference', prompt)
        self.assertIn(canonical(value).decode(), prompt)
        self.assertIn('Choose your own commands', prompt)
        self.assertIn('normal (non-draft) GitHub PR', prompt)
        self.assertIn('description in Japanese', prompt)
        self.assertIn('actual tests and unverified limitations', prompt)
        self.assertIn('body containing your nonempty public PR description', prompt)
        self.assertIn('No manual ready-for-review step is needed', prompt)
        self.assertIn('never merges', prompt)
        self.assertIn('never create a replacement or repeat the research', prompt)

    def test_direct_response_requires_pr_url_without_changing_legacy_schema(self):
        result, transport = self.run_candidate(dict(outcome='completed', summary='Done', pr_url=PR_URL))
        turn = next(frame for frame in transport.sent if frame.get('method') == 'turn/start')
        self.assertEqual(turn['params']['outputSchema'], DIRECT_PR_RESPONSE_SCHEMA)
        self.assertIn('pr_url', DIRECT_PR_RESPONSE_SCHEMA['required'])
        self.assertNotIn('pr_url', GOAL_RESPONSE_SCHEMA['properties'])
        self.assertEqual(result.pr_url, PR_URL)
        self.assertFalse(result.independently_validated)

    def test_null_pr_url_is_a_valid_incomplete_report(self):
        result, _ = self.run_candidate(dict(outcome='incomplete', summary='No PR confirmed', pr_url=None))
        self.assertIsNone(result.pr_url)
        self.assertEqual(result.outcome, 'incomplete')
        self.assertFalse(result.independently_validated)

    def test_missing_forged_or_foreign_pr_url_is_rejected(self):
        candidates = [dict(outcome='completed', summary='Missing URL')]
        for url in (True, '', 'https://github.com/other/repo/pull/123',
                    f'https://github.com/{REPOSITORY}/pull/0', PR_URL + '?verified=true'):
            candidates.append(dict(outcome='completed', summary='Untrusted', pr_url=url))
        candidates.append(dict(outcome='completed', summary='Forged', pr_url=PR_URL, pr_verified=True))
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ContractError):
                    self.run_candidate(candidate)


class DirectServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        initialize(self.root)
        self.state = ServiceState(self.root, require_epochs=True)
        self.addCleanup(lambda: self.state.close())
        self.source = direct_source()
        self.issues = self.source
        self.runtime = DirectRuntime()
        self.now = FIXTURE_NOW
        self.mono = 1000
        initial = self.source.request['plan']
        self.profile = dict(model=initial['model'], direct_pr_scope=direct_pr_scope(initial))
        self.installed_digest = hashlib.sha256(canonical(initial)).hexdigest()
        self.publisher = Mock(side_effect=AssertionError('Manager must never construct a publisher'))
        self.publication_base = Mock(side_effect=AssertionError('Manager must not select a publication base'))
        self.addCleanup(self.publisher.assert_not_called)
        self.addCleanup(self.publication_base.assert_not_called)
        self.make()

    def make(self, *, enabled=True, config=None):
        self.controller = DirectPRController(self.state, self.issues, self.issues,
            self.runtime, self.publisher, source_sha=SOURCE_SHA,
            publication_base_getter=self.publication_base, clock=lambda: self.now,
            monotonic=lambda: self.mono, enabled=enabled, deployment_id='1' * 32,
            installed_plan_digest=self.installed_digest, resource_limits=LIMITS,
            profile=self.profile, worker_pr_config=enabled_config() if config is None else config)

    def reopen(self, *, newer_epoch=False):
        self.state.close()
        self.state = ServiceState(self.root, require_epochs=True)
        if newer_epoch:
            self.runtime.epoch = epoch(pid1_start_ticks=self.runtime.epoch.pid1_start_ticks + 1000)
        self.make()

    def assert_not_claimed(self):
        self.assertIsNone(self.state.get(ISSUE))
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(list((self.root / 'claims').iterdir()), [])

    def test_finished_result_is_durable_but_never_verified_publication(self):
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'WORKER_FINISHED')
        self.assertTrue(report['dispatched'])
        self.assertTrue(report['restart_required'])
        self.assertEqual(report['notification'], 'EXTERNAL_PR_EVENT')
        self.assertEqual(report['pr_url'], PR_URL)
        self.assertIs(report['pr_verified'], False)
        self.assertIs(report['research_success_verified'], False)
        saved = self.state.get(ISSUE)
        self.assertEqual(saved['result']['pr_url'], PR_URL)
        self.assertEqual(saved['result']['binding'], saved['binding'])
        self.assertIsNone(saved['publication'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(stat.S_IMODE((self.root / 'claims' / f'{ISSUE}.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'state.sqlite').stat().st_mode), 0o600)

    def test_dedup_survives_same_and_new_epoch_reopen(self):
        self.controller.process(ISSUE)
        for newer in (False, True):
            with self.subTest(newer_epoch=newer):
                self.reopen(newer_epoch=newer)
                report = self.controller.process(ISSUE)
                self.assertEqual(report['phase'], 'WORKER_FINISHED')
                self.assertTrue(report['duplicate'])
                self.assertIs(report['dispatched'], False)
                self.assertIs(report['pr_verified'], False)
                self.assertEqual(self.runtime.runs, 1)
                self.assertEqual(self.runtime.preflights, 1)

    def test_two_different_approved_goals_dispatch_across_fake_epochs(self):
        second = direct_source(ISSUE + 1)
        second.request['plan']['goal'] = 'Run a separately approved CPU experiment.'
        second.request['plan']['completion_criteria'] = ['Report failed experiments too.']
        second.render(refresh_receipt=True)
        self.issues = MultipleSources(self.source, second)
        self.make()
        first_reports = self.controller.once()
        self.assertEqual([report['issue'] for report in first_reports], [ISSUE])
        self.assertEqual(self.runtime.runs, 1)
        self.assertIsNone(self.state.get(ISSUE + 1))
        self.reopen(newer_epoch=True)
        second_reports = self.controller.once()
        self.assertEqual([report['issue'] for report in second_reports], [ISSUE, ISSUE + 1])
        self.assertIs(second_reports[0]['dispatched'], False)
        self.assertIs(second_reports[1]['dispatched'], True)
        self.assertEqual(self.runtime.runs, 2)
        self.assertNotEqual(self.runtime.seen_bindings[0]['plan_digest'],
                            self.runtime.seen_bindings[1]['plan_digest'])
        epochs = self.state.db.execute('SELECT before_epoch FROM service_epochs ORDER BY issue').fetchall()
        self.assertNotEqual(epochs[0], epochs[1])
        self.reopen(newer_epoch=True)
        self.controller.once()
        self.assertEqual(self.runtime.runs, 2)

    def test_controller_and_worker_config_each_disable_before_reads(self):
        for enabled, config in ((False, enabled_config()), (True, default_worker_pr_config())):
            with self.subTest(enabled=enabled):
                self.make(enabled=enabled, config=config)
                report = self.controller.process(ISSUE)
                self.assertEqual(report['phase'], 'DIRECT_PR_DISABLED')
                self.assertIs(report['dispatched'], False)
                self.assertEqual(self.source.reads, 0)
                self.assertEqual(self.runtime.preflights, 0)
                self.assert_not_claimed()

    def test_modified_approval_is_rejected_before_preflight(self):
        self.source.request['plan']['goal'] = 'Changed without a new owner receipt'
        self.source.render()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)

    def test_revoked_approval_is_rejected_before_preflight(self):
        self.source.revoke()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)

    def test_expired_approval_is_rejected_before_preflight(self):
        self.now = timestamp(self.source.request['expires_at'])
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)

    def test_issue_target_must_match_actual_approved_issue(self):
        self.source.request['plan']['direct_pr']['issue_number'] = ISSUE + 1
        self.source.render(refresh_receipt=True)
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_not_claimed()

    def test_owner_approval_cannot_expand_installed_envelope(self):
        self.source.request['plan']['write_scope'].append('elsewhere')
        self.source.render(refresh_receipt=True)
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)

    def test_approval_change_during_preflight_prevents_claim(self):
        def changed():
            self.source.request['plan']['goal'] = 'Unapproved replacement goal'
            self.source.render()
        self.runtime.on_preflight = changed
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 1)

    def test_revocation_during_run_blocks_claim_and_never_reruns(self):
        def revoke():
            self.source.revoke()
            self.now += 6
            self.mono += 6
        self.runtime.on_run = revoke
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.reopen(newer_epoch=True)
        self.assertEqual(self.controller.process(ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(self.runtime.runs, 1)

    def test_failed_execution_claim_remains_terminal_after_reopen(self):
        self.runtime.run_error = RuntimeError('uncertain worker result')
        with self.assertRaises(RuntimeError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.runtime.run_error = None
        self.reopen(newer_epoch=True)
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'BLOCKED')
        self.assertIs(report['dispatched'], False)
        self.assertEqual(self.runtime.runs, 1)

    def test_existing_claim_without_result_never_dispatches_again(self):
        binding = self.controller.approve(ISSUE)
        self.assertTrue(self.state.claim(binding, epoch=self.runtime.epoch))
        self.reopen(newer_epoch=True)
        report = self.controller.process(ISSUE)
        self.assertTrue(report['duplicate'])
        self.assertIs(report['dispatched'], False)
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.runtime.preflights, 0)

    def test_losing_claim_race_never_dispatches(self):
        original = self.state.claim
        def lose(binding, epoch):
            self.assertTrue(original(binding, epoch=epoch))
            return False
        with patch.object(self.state, 'claim', side_effect=lose):
            with self.assertRaises(ContractError):
                self.controller.process(ISSUE)
        self.reopen(newer_epoch=True)
        self.assertIs(self.controller.process(ISSUE)['dispatched'], False)
        self.assertEqual(self.runtime.runs, 0)

    def test_null_reported_url_does_not_trigger_publication_or_retry(self):
        self.runtime.result_transform = lambda result: replace(result, outcome='incomplete', pr_url=None)
        report = self.controller.process(ISSUE)
        self.assertIsNone(report['pr_url'])
        self.assertIs(report['pr_verified'], False)
        self.reopen(newer_epoch=True)
        self.assertIs(self.controller.process(ISSUE)['dispatched'], False)
        self.assertEqual(self.runtime.runs, 1)

    def test_forged_result_model_is_rejected(self):
        self.runtime.result_transform = lambda result: replace(result, model='uninstalled-model')
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertIsNone(self.state.get(ISSUE)['result'])

    def test_forged_result_binding_is_rejected(self):
        self.runtime.result_transform = lambda result: replace(
            result, binding=dict(result.binding, run_id='different-run'))
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertIsNone(self.state.get(ISSUE)['result'])

    def test_saved_report_cannot_add_verified_pr_assertion(self):
        self.controller.process(ISSUE)
        saved = self.state.get(ISSUE)['result']
        saved['pr_verified'] = True
        with self.assertRaises(ContractError):
            DirectPRResult.from_dict(saved)


class DirectSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.os = GoalOS()
        self.supervisor = runtime.GoalUIDSupervisor(self.os)
        self.request = GoalRequest(direct_plan(), SOURCE_SHA)
        self.binding = runtime.GoalRunBinding(SOURCE_SHA, self.request.digest, 'request-1', 'run-1')
        self.runner = Mock()
        self.runner.run.return_value = GoalCandidate('completed', 'Worker report', 't', 'u', pr_url=PR_URL)
        self.transport = Mock()
        self.quarantine = Mock(side_effect=AssertionError('Direct mode must skip quarantine'))
        self.os.collect_goal = Mock(side_effect=AssertionError('Direct mode must not collect candidate files'))

    def run_goal(self, **kwargs):
        with patch.object(runtime, 'GoalAppServerRunner', return_value=self.runner), \
             patch.object(runtime, 'BoundedStdioTransport', return_value=self.transport) as factory:
            self.transport_factory = factory
            return self.supervisor.run_goal(self.request, lambda: None,
                binding=self.binding, quarantine=self.quarantine, enabled=True, **kwargs)

    def test_direct_mode_preserves_watchdog_uid_cleanup_and_skips_quarantine(self):
        result = self.run_goal()
        self.assertEqual(result.candidate.pr_url, PR_URL)
        self.assertTrue(result.requires_namespace_restart)
        self.assertFalse(result.cleanup_verified)
        self.assertFalse(result.publish_authorized)
        self.assertFalse(result.candidate.independently_validated)
        self.assertIn(('arm', 124), self.os.events)
        self.assertEqual(self.transport_factory.call_args.kwargs['deadline'], 100)
        self.assertTrue(any(event[0] == 'signal' for event in self.os.events))
        self.assertFalse(any(event[0] == 'disarm' for event in self.os.events))
        self.assertNotIn(10, self.os.members)
        self.assertIn(('close_child',), self.os.events)
        self.transport.close.assert_called_once()
        self.quarantine.assert_not_called()
        self.os.collect_goal.assert_not_called()
        with self.assertRaises(ContractError):
            self.run_goal()

    def test_direct_mode_rejects_wrong_uid_before_releasing(self):
        original = self.os.launch_goal
        def wrong_uid(request):
            handle = original(request)
            self.os.members[10] = replace(self.os.members[10], uids=(0,) * 4)
            return handle
        self.os.launch_goal = wrong_uid
        with self.assertRaises(NamespaceExit):
            self.run_goal()
        self.assertIn(('namespace_exit', 125), self.os.events)
        self.assertNotIn(('release',), self.os.events)
        self.runner.run.assert_not_called()
        self.quarantine.assert_not_called()
        self.assertFalse(any(event[0] == 'disarm' for event in self.os.events))

    def test_direct_mode_failure_still_closes_and_stops_owned_worker(self):
        self.runner.run.side_effect = ContractError('offline turn failed')
        with self.assertRaises(ContractError):
            self.run_goal()
        self.assertNotIn(10, self.os.members)
        self.assertIn(('close_child',), self.os.events)
        self.transport.close.assert_called_once()
        self.quarantine.assert_not_called()
        self.os.collect_goal.assert_not_called()
        self.assertFalse(any(event[0] == 'disarm' for event in self.os.events))



def direct_profile(value=None):
    value = direct_plan() if value is None else value
    profile = prepare.profile_template()
    profile.update(enabled=True, deployment_id=value['deployment_id'],
        input_source_sha=SOURCE_SHA, runtime_source_sha='c' * 40,
        model=value['model'], resource_limits=deepcopy(LIMITS))
    profile['risk_acknowledgements'] = dict.fromkeys(prepare.CONSENTS, True)
    return profile


class DirectEntrypointTests(unittest.TestCase):
    def test_worker_wrapper_help_from_checkout_without_ambient_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, '-I', '-B',
                str(root / 'tools/research_goal/worker_pr.py'), '--help'],
                cwd=folder, capture_output=True, text=True, timeout=10, check=True)
        self.assertIn('--approved-plan', result.stdout)
        self.assertIn('--manifest', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_worker_wrapper_help_from_simulated_installed_layout(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            installed = Path(folder) / 'opt/service'
            installed.mkdir(parents=True)
            shutil.copy2(root / 'tools/research_goal/worker_pr.py', installed / 'worker_pr.py')
            shutil.copytree(root / 'research_harness', installed / 'research_harness',
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            result = subprocess.run([sys.executable, '-I', '-B',
                str(installed / 'worker_pr.py'), '--help'], cwd=folder,
                capture_output=True, text=True, timeout=10, check=True)
        self.assertIn('--approved-plan', result.stdout)
        self.assertIn('--intent', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_worker_pr_template_is_disabled_and_cannot_launch_subprocess(self):
        output = io.StringIO()
        with patch.object(prepare.subprocess, 'run') as run, \
             patch.object(prepare.subprocess, 'Popen') as popen, redirect_stdout(output):
            prepare.main(['--worker-pr-template'])
        self.assertEqual(json.loads(output.getvalue()), default_worker_pr_config())
        self.assertIs(json.loads(output.getvalue())['enabled'], False)
        self.assertTrue(all(json.loads(output.getvalue())[key] is False for key in CONSENTS))
        run.assert_not_called()
        popen.assert_not_called()

    def test_installed_profile_records_envelope_instead_of_pinning_future_goal(self):
        original = direct_plan()
        installed = prepare.installed_profile(direct_profile(original), original)
        self.assertEqual(installed['direct_pr_scope'], direct_pr_scope(original))
        self.assertEqual(installed['installed_plan_digest'], hashlib.sha256(canonical(original)).hexdigest())
        another = direct_plan(ISSUE + 1)
        another['goal'] = 'An independently approved new research question.'
        another['direct_pr']['title'] = '[Codex] New results'
        require_installed_goal(installed, another)
        launcher.require_installed_plan(installed, another)
        for key in ('inputs', 'write_scope'):
            with self.subTest(key=key):
                changed = deepcopy(another)
                changed[key].append('outside-installed-envelope')
                with self.assertRaises(ContractError):
                    launcher.require_installed_plan(installed, changed)


class DirectPipeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.plan = direct_plan()
        self.profile = prepare.installed_profile(direct_profile(self.plan), self.plan)
        self.binding = dict(issue_number=ISSUE, issue_content_sha256='d' * 64,
            request_id='request-1', run_id='run-1', source_sha=SOURCE_SHA,
            plan_digest=hashlib.sha256(canonical(self.plan)).hexdigest())
        self.result = DirectPRResult(self.binding, self.plan['model'], 'completed', 'Reported only', PR_URL, .5)

    def pipe(self, *frames):
        incoming = io.StringIO(''.join(canonical(frame).decode() + '\n' for frame in frames))
        outgoing = io.StringIO()
        runtime = launcher.GoalPipeRuntime(incoming, outgoing, epoch(), self.profile)
        return runtime, outgoing

    def test_direct_result_crosses_pipe_with_rechecks_and_no_quarantine_read(self):
        runtime, outgoing = self.pipe({'prepared': True}, {'ready': True},
            {'op': 'recheck'}, {'direct_pr_result': self.result.to_dict()})
        recheck = Mock()
        with patch.object(launcher, 'protected_json') as quarantine_read:
            runtime.prepare_plan(self.plan)
            self.assertEqual(runtime.preflight(), epoch())
            result = runtime.run(self.binding, recheck)
        self.assertEqual(result, self.result)
        recheck.assert_called_once_with()
        quarantine_read.assert_not_called()
        sent = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([frame.get('op') for frame in sent[:3]], ['prepare_goal', 'preflight', 'goal'])
        self.assertEqual(sent[-1], {'approved': True})

    def test_direct_pipe_rejects_wrong_binding_model_and_verified_extra_field(self):
        values = [replace(self.result, binding=dict(self.binding, run_id='wrong')).to_dict(),
                  replace(self.result, model='different-model').to_dict(),
                  dict(self.result.to_dict(), pr_verified=True)]
        for value in values:
            with self.subTest(value=value):
                runtime, _ = self.pipe({'direct_pr_result': value})
                runtime.plan = self.plan
                with self.assertRaises(ContractError):
                    runtime.run(self.binding, lambda: None)

    def test_direct_result_is_rejected_by_legacy_pipe_mode(self):
        runtime, _ = self.pipe({'direct_pr_result': self.result.to_dict()})
        runtime.plan = plan()
        with self.assertRaises(ContractError):
            runtime.run(self.binding, lambda: None)

    def test_manager_direct_branch_never_constructs_publisher_transport(self):
        import research_harness.direct_pr_service as service
        import research_harness.goal_publisher as publisher
        import research_harness.manager_github as github
        import research_harness.owner_approval as approval
        state = Mock()
        pipe = Mock(failed_issue=None)
        controller = Mock(generation_attempted=True)
        controller.once.return_value = [dict(issue=ISSUE, phase='WORKER_FINISHED', restart_required=True)]
        profile = self.profile
        def protected(path, *args):
            if path == '/control/goal-runtime.json':
                return profile
            if path == '/control/worker-pr.json':
                return enabled_config()
            self.fail('Unexpected protected read: ' + str(path))
        with ExitStack() as stack:
            def mocked(obj, name, **kwargs):
                return stack.enter_context(patch.object(obj, name, **kwargs))
            mocked(launcher.os, 'getresuid', return_value=(10001,) * 3)
            mocked(launcher.os, 'umask')
            mocked(launcher, 'read_frame', return_value={'epoch': epoch().to_dict()})
            mocked(launcher, 'protected_json', side_effect=protected)
            mocked(launcher, 'initialize_manager_state', return_value=Path('/offline/state'))
            mocked(launcher, 'ServiceState', return_value=state)
            mocked(launcher, 'GoalPipeRuntime', return_value=pipe)
            mocked(github, 'ManagerGitHub')
            mocked(approval, 'GitHubOwnerComments')
            factory = mocked(service, 'DirectPRController', return_value=controller)
            publish = mocked(publisher, 'GitHubGoalPublisher')
            transport = mocked(publisher, 'GhGoalTransport')
            write = mocked(launcher, 'write_frame')
            launcher.manager()
        self.assertIsNone(factory.call_args.args[4])
        self.assertEqual(factory.call_args.kwargs['profile'], profile)
        self.assertNotIn('publication_base_getter', factory.call_args.kwargs)
        publish.assert_not_called()
        transport.assert_not_called()
        self.assertEqual(write.call_args_list[-1].args[1], {'op': 'restart'})
        state.close.assert_called_once()


class DirectCredentialStagingTests(unittest.TestCase):
    def test_actual_owner_token_is_refused_before_any_worker_credential_copy(self):
        # Fake bytes, fake file metadata and fake actor response only. No token
        # file, UID change, network request, or credential installation occurs.
        value = direct_plan()
        value['inputs'] = []
        manifest = canonical(dict(source_sha=SOURCE_SHA, files={}))
        class FakePath:
            def __init__(self, path): self.path = PurePosixPath(path)
            def __truediv__(self, part): return FakePath(self.path / part)
            def __str__(self): return str(self.path)
            def mkdir(self, **kwargs): pass
            def read_bytes(self):
                if str(self) == '/opt/c3-runtime/goal-inputs.json': return manifest
                raise AssertionError('Unexpected fake read: ' + str(self))
        fake_auth = b'OFFLINE_AUTH_BYTES'
        fake_token = b'OFFLINE_SYNTHETIC_TOKEN'
        records = {'/control/codex-auth.json': fake_auth,
            '/control/worker-pr.json': canonical(enabled_config()),
            '/control/worker-github-token': fake_token}
        uid = Mock()
        client = Mock()
        client.request.return_value = dict(login=OWNER_LOGIN, id=OWNER_ID)
        with patch.object(direct_runtime, 'Path', side_effect=FakePath), \
             patch.object(direct_runtime.os, 'listdir', return_value=[]), \
             patch.object(direct_runtime.os, 'umask'), \
             patch.object(direct_runtime, '_worker_directory') as directory, \
             patch.object(direct_runtime, '_write_new') as write, \
             patch.object(model_deployment, 'read_private', side_effect=lambda path, **kw: records[str(path)]) as read, \
             patch.object(model_deployment, 'validate_chatgpt_auth') as auth_check, \
             patch.object(direct_runtime, 'WorkerGitHub', return_value=client) as github:
            with self.assertRaisesRegex(ContractError, 'actual worker credential identity mismatch'):
                direct_runtime.stage_direct_workspace(value, as_uid=uid)
        self.assertEqual(directory.call_count, 3)
        self.assertEqual(read.call_count, 3)
        auth_check.assert_called_once_with(fake_auth)
        github.assert_called_once_with(fake_token.decode())
        client.request.assert_called_once_with('GET', '/user')
        write.assert_not_called()
        self.assertEqual([call.args[0] for call in uid.call_args_list], [10001, 0])


class DirectWorkerHomeTests(unittest.TestCase):
    def setUp(self):
        self.plan = direct_plan()
        self.records = {'worker-pr.json': canonical(enabled_config()),
            'github-token': b'OFFLINE_SYNTHETIC_TOKEN', 'approved-plan.json': canonical(self.plan)}
        self.overrides = {}
        self.opened_overrides = {}
        self.opened = []
        self.reads = []
        self.closed = []

    def validate(self):
        owner = self
        class WorkerPath:
            def __init__(self, value): self.path = PurePosixPath(value)
            def __truediv__(self, part): return WorkerPath(self.path / part)
            def __str__(self): return str(self.path)
            def lstat(self):
                values = dict(st_mode=stat.S_IFREG | 0o600, st_uid=10002, st_nlink=1,
                    st_size=len(owner.records[self.path.name]), st_dev=7, st_ino=17)
                values.update(owner.overrides)
                return SimpleNamespace(**values)
        def opened(path, flags):
            self.opened.append((str(path), flags))
            return 100 + list(self.records).index(path.path.name)
        def fstat(fd):
            values = dict(st_dev=7, st_ino=17)
            values.update(self.opened_overrides)
            return SimpleNamespace(**values)
        def read(fd, maximum):
            self.reads.append((fd, maximum))
            return list(self.records.values())[fd - 100]
        with patch.object(direct_runtime, 'Path', side_effect=WorkerPath), \
             patch.object(direct_runtime.os, 'listdir', return_value=list(self.records)), \
             patch.object(direct_runtime.os, 'open', side_effect=opened), \
             patch.object(direct_runtime.os, 'fstat', side_effect=fstat), \
             patch.object(direct_runtime.os, 'read', side_effect=read), \
             patch.object(direct_runtime.os, 'close', side_effect=self.closed.append), \
             patch.object(model_deployment, 'read_private') as controller_reader:
            try:
                return direct_runtime.validate_worker_home(self.plan)
            finally:
                controller_reader.assert_not_called()

    def test_worker_owned_config_plan_and_token_are_read_back(self):
        self.assertIsNone(self.validate())
        self.assertEqual([path for path, _ in self.opened], [
            '/work/worker-home/worker-pr.json', '/work/worker-home/approved-plan.json',
            '/work/worker-home/github-token'])
        self.assertTrue(all(flags & os.O_NOFOLLOW for _, flags in self.opened))
        self.assertEqual(len(self.reads), 3)
        self.assertEqual(len(self.closed), 3)

    def test_controller_or_root_owned_file_rejected_before_open(self):
        for uid in (0, 10001):
            with self.subTest(uid=uid):
                self.overrides = {'st_uid': uid}
                with self.assertRaisesRegex(ContractError, 'layout invalid'):
                    self.validate()
                self.assertEqual(self.opened, [])
                self.assertEqual(self.reads, [])

    def test_group_readable_or_hardlinked_worker_file_rejected(self):
        for changes in ({'st_mode': stat.S_IFREG | 0o640}, {'st_nlink': 2}):
            with self.subTest(changes=changes):
                self.overrides = changes
                with self.assertRaisesRegex(ContractError, 'layout invalid'):
                    self.validate()
                self.assertEqual(self.opened, [])

    def test_replaced_inode_is_rejected_and_fd_closed_without_reading(self):
        self.opened_overrides = {'st_ino': 18}
        with self.assertRaisesRegex(ContractError, 'changed'):
            self.validate()
        self.assertEqual(self.reads, [])
        self.assertEqual(len(self.closed), 1)

    def test_disabled_readback_config_cannot_enable_direct_worker(self):
        self.records['worker-pr.json'] = canonical(default_worker_pr_config())
        with self.assertRaisesRegex(ContractError, 'config/plan mismatch'):
            self.validate()
        self.assertEqual(len(self.reads), 1)

    def test_changed_approved_plan_or_missing_token_rejected(self):
        self.records['approved-plan.json'] = canonical(dict(self.plan, goal='Unapproved replacement'))
        with self.assertRaisesRegex(ContractError, 'config/plan mismatch'):
            self.validate()
        self.records['approved-plan.json'] = canonical(self.plan)
        self.records['github-token'] = b'\n'
        with self.assertRaisesRegex(ContractError, 'credential missing'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
