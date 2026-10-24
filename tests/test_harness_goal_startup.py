"""Real filesystem regressions with modeled Linux real/effective/saved UIDs.

No privilege changes, containers, credentials, network or model calls.
"""
from contextlib import ExitStack, contextmanager
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tests.test_harness_goal_deployment import (launcher, prepare, goal_fixture,
    profile_fixture, goal_inspect, volume_fixture, CONTAINER_ID, SOURCE_SHA,
    VOLUME_NAME, GoalQuarantineTests)
from research_harness.persistent_fixture import private
from research_harness.schema import ContractError, canonical
from research_harness.restart_boundary import RestartEpoch
from research_harness.service_state import ServiceState


@contextmanager
def control_identity(root, *, real=0):
    """Model owner10001 and seteuid without hiding filesystem validation."""
    original_lstat = Path.lstat
    identity = [real, real, real]
    def lstat(path):
        result = original_lstat(path)
        if path == root or root in path.parents:
            values = list(result); values[4] = 10001
            return os.stat_result(values)
        return result
    def switch(uid): identity[1] = uid
    with ExitStack() as stack:
        stack.enter_context(patch.object(Path, 'lstat', lstat))
        stack.enter_context(patch.object(launcher.os, 'getuid', lambda: identity[0]))
        stack.enter_context(patch.object(launcher.os, 'geteuid', lambda: identity[1]))
        stack.enter_context(patch.object(launcher.os, 'getresuid', lambda: tuple(identity)))
        stack.enter_context(patch.object(launcher, 'as_uid', switch))
        yield identity


class GoalStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = self.root/'control'; self.control.mkdir(mode=0o700)
        self.profile = prepare.installed_profile(profile_fixture(), goal_fixture())
        self.write('goal-runtime.json', self.profile)
        self.write('goal-deployment.json', dict(inspect=goal_inspect(), consumers=[CONTAINER_ID],
            volume_name=VOLUME_NAME, volume_inspect={**volume_fixture(), 'Labels':{'c3.deployment-id':'1'*32}},
            initialize_new_state=True))
        (self.root/'source-sha').write_text(SOURCE_SHA)
        (self.root/'hostname').write_text(CONTAINER_ID[:12])
        self.epoch = RestartEpoch(CONTAINER_ID, '11111111-1111-1111-1111-111111111111',22,33)

    def write(self, name, value):
        path = self.control/name; path.write_bytes(canonical(value)); path.chmod(0o600)

    def path(self, value):
        value = str(value)
        if value == '/control' or value.startswith('/control/'):
            return self.control/value.removeprefix('/control').lstrip('/')
        if value == '/opt/service/source-sha': return self.root/'source-sha'
        if value == '/etc/hostname': return self.root/'hostname'
        return Path(value)

    def deploy(self):
        with patch.object(launcher, 'Path', side_effect=self.path), patch.object(launcher.os, 'getpid', return_value=1):
            return launcher.deployment()

    def test_deployment_real_root_effective_manager_passes_actual_validation(self):
        with control_identity(self.control) as ids:
            ids[1] = 10001
            with self.assertRaises(ContractError): private(self.control, True)
            launcher.protected_control_directory(self.control)
            ids[1] = 0
            config, profile, new = self.deploy()
            self.assertEqual(config['container_id'], CONTAINER_ID)
            self.assertEqual(profile, self.profile); self.assertIs(new, True)
            self.assertEqual(ids, [0,0,0])

    def test_invalid_control_owner_mode_type_and_effective_identity_rejected(self):
        from types import SimpleNamespace
        for mode, owner, effective in [(stat.S_IFDIR|0o700,0,10001),
                (stat.S_IFDIR|0o755,10001,10001), (stat.S_IFLNK|0o700,10001,10001),
                (stat.S_IFREG|0o700,10001,10001), (stat.S_IFDIR|0o700,10001,0)]:
            path = SimpleNamespace(lstat=lambda:SimpleNamespace(st_mode=mode,st_uid=owner))
            with self.subTest(mode=mode,owner=owner,effective=effective), \
                 patch.object(launcher.os, 'geteuid', return_value=effective), self.assertRaises(ContractError):
                launcher.protected_control_directory(path)

    def test_rejection_restores_root_and_reports_exact_safe_stage(self):
        for name, stage in [('goal-runtime.json','profile'), ('goal-deployment.json','deployment_manifest')]:
            saved = (self.control/name).read_bytes()
            (self.control/name).write_text('PRIVATE SECRET INVALID JSON')
            with control_identity(self.control) as ids, self.assertRaises(launcher.GoalStartupFailure) as caught:
                self.deploy()
            self.assertEqual(ids, [0,0,0]); self.assertEqual(caught.exception.stage, stage)
            with patch.object(launcher.sys, 'stderr', new_callable=io.StringIO) as output:
                launcher.report_startup_failure('launcher', caught.exception)
            self.assertNotIn('PRIVATE', output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['stage'], stage)
            (self.control/name).write_bytes(saved)

    def test_fresh_manager_initialization_and_resume_open_real_service_state(self):
        with control_identity(self.control, real=10001), patch.object(launcher, 'Path', side_effect=self.path):
            root = launcher.initialize_manager_state({'initialize_new_state':True}, self.epoch)
            self.assertIs(json.loads((self.control/'goal-deployment.json').read_bytes())['initialize_new_state'], False)
            marker = (root/'initialized.json').read_bytes()
            state = ServiceState(root, require_epochs=True); state.close()
            resumed = launcher.initialize_manager_state({'initialize_new_state':False}, self.epoch)
            self.assertEqual(resumed, root)
            self.assertEqual((root/'initialized.json').read_bytes(), marker)
            with self.assertRaises(ContractError):
                launcher.initialize_manager_state({'initialize_new_state':True}, self.epoch)
            state = ServiceState(root, require_epochs=True); state.close()

    def test_deployment_hello_to_manager_initialization_and_restart_frames(self):
        from types import SimpleNamespace
        with control_identity(self.control):
            config, profile, new = self.deploy()
        hello = io.BytesIO()
        launcher.write_frame(hello, {'epoch':self.epoch.to_dict(), 'initialize_new_state':new})
        hello.seek(0)
        controller = SimpleNamespace(generation_attempted=False,
            once=lambda:[{'restart_required':True}])
        outgoing = io.BytesIO()
        old_mask = os.umask(0o077)
        try:
            with control_identity(self.control, real=10001), \
                 patch.object(launcher, 'Path', side_effect=self.path), \
                 patch.object(launcher.sys, 'stdin', hello), \
                 patch.object(launcher.sys, 'stdout', outgoing), \
                 patch('research_harness.manager_github.ManagerGitHub'), \
                 patch('research_harness.owner_approval.GitHubOwnerComments'), \
                 patch('research_harness.goal_publisher.GhGoalTransport'), \
                 patch('research_harness.goal_service.GoalController', return_value=controller):
                launcher.manager()
                state = ServiceState(self.control/'state', require_epochs=True)
                state.close()
        finally:
            os.umask(old_mask)
        outgoing.seek(0)
        self.assertEqual(launcher.read_frame(outgoing), {'op':'report','reports':[{'restart_required':True}]})
        self.assertEqual(launcher.read_frame(outgoing), {'op':'restart'})
        self.assertIs(json.loads((self.control/'goal-deployment.json').read_bytes())['initialize_new_state'], False)

    def test_all_startup_stages_and_error_reasons_are_bounded(self):
        for stage in launcher.STARTUP_STAGES:
            for error, reason in [(ContractError('PRIVATE'), 'CONTRACT_REJECTED'),
                    (PermissionError('PRIVATE'), 'PERMISSION_DENIED'),
                    (FileNotFoundError('PRIVATE'),'FILE_MISSING'), (OSError('PRIVATE'),'IO_FAILED'),
                    (ValueError('PRIVATE'),'DATA_INVALID'), (RuntimeError('PRIVATE'),'STARTUP_FAILED')]:
                with self.assertRaises(launcher.GoalStartupFailure) as caught:
                    with launcher.startup_stage(stage): raise error
                with patch.object(launcher.sys, 'stderr', new_callable=io.StringIO) as output:
                    launcher.report_startup_failure('manager', caught.exception)
                self.assertEqual(json.loads(output.getvalue()), dict(phase='GOAL_BLOCKED_STARTUP',role='manager',stage=stage,reason=reason))

    def test_supervisor_epoch_failure_has_epoch_stage(self):
        with patch.object(launcher, 'deployment', return_value=({'container_id':CONTAINER_ID}, {}, False)), \
             patch.object(launcher, 'read_epoch', side_effect=ContractError('PRIVATE')), \
             patch.object(launcher, 'blocked_startup') as blocked, \
             patch.object(launcher.subprocess, 'Popen') as spawn, \
             patch.object(launcher.sys, 'stderr', new_callable=io.StringIO) as output:
            launcher.supervise()
        self.assertEqual(json.loads(output.getvalue())['stage'], 'epoch')
        spawn.assert_not_called(); blocked.assert_called_once()


class GoalEffectiveIdentityQuarantineTests(GoalQuarantineTests):
    def invoke(self, *, readback=None):
        actual_path = Path
        def path(value):
            return self.root if str(value) == '/control/quarantine' else actual_path(value)
        with control_identity(self.root) as ids, patch.object(launcher, 'Path', side_effect=path):
            if readback is None:
                result = launcher.freeze_snapshot(self.snapshot, self.binding, self.baseline, self.plan)
            else:
                with patch.object(launcher, 'protected_json', side_effect=readback):
                    result = launcher.freeze_snapshot(self.snapshot, self.binding, self.baseline, self.plan)
            self.assertEqual(ids, [0,0,0])
            return result
