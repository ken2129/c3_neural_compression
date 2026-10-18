"""Real temporary bytes/modes, virtual UID metadata; not Docker isolation proof.

No real credentials, UID changes, model calls, network, or deployment occur.
"""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import research_harness.direct_pr_runtime as direct_runtime
import research_harness.model_deployment as model_deployment
from research_harness.schema import ContractError, canonical
from research_harness.worker_pr import OWNER_ID, OWNER_LOGIN
from test_harness_direct_pr import direct_plan, enabled_config, SOURCE_SHA
from tests.test_harness_goal_deployment import prepare


class DirectFilesystemStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.real_euid = os.geteuid()
        self.uid = 0
        self.uid_events = []
        self.owner = {}
        self.writes = []
        self.config = enabled_config()
        self.plan = direct_plan()
        self.plan['inputs'] = ['data/public.csv', 'output/nested/editable.py']
        self.plan['write_scope'] = ['output', 'reports/research']
        self.input_bytes = {'data/public.csv': b'epoch,loss\n1,2\n',
            'output/nested/editable.py': b'print("offline seed")\n'}
        self.manifest = dict(source_sha=SOURCE_SHA, files={
            path: dict(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            for path, raw in self.input_bytes.items()})
        for name in ('work', 'control', 'opt/c3-runtime', 'opt/goal-inputs'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for path, raw in self.input_bytes.items():
            target = self.root / 'opt/goal-inputs' / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o444)
        (self.root / 'opt/c3-runtime/goal-inputs.json').write_bytes(canonical(self.manifest))
        fake_records = {'codex-auth.json': b'{"offline_fixture":true}',
            'worker-pr.json': canonical(self.config),
            'worker-github-token': b'OFFLINE_SYNTHETIC_TOKEN'}
        for name, raw in fake_records.items():
            path = self.root / 'control' / name
            path.write_bytes(raw)
            path.chmod(0o600)
            self.owner[str(path)] = 10001
        self.client = Mock()
        self.client.request.return_value = dict(id=self.config['actor_id'], login=self.config['actor_login'])
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)

    def set_uid(self, uid):
        self.uid_events.append(uid)
        self.uid = uid

    def local(self, path):
        path = Path(path)
        if path.is_absolute() and path.parts[1] in ('work', 'control', 'opt'):
            return self.root / path.relative_to('/')
        return path

    def fixture(self):
        """Run real staging functions against remapped local paths/UID labels."""
        owner = self
        original_listdir = os.listdir
        original_write = direct_runtime._write_new
        class VirtualPath:
            # Composition avoids pathlib's version-dependent subclass
            # construction while all bytes and modes still use real Paths.
            def __init__(self, *args):
                self.path = owner.local(Path(*args))

            def __fspath__(self):
                return os.fspath(self.path)

            def __str__(self):
                return str(self.path)

            def __truediv__(self, part):
                return VirtualPath(self.path / part)

            @property
            def parent(self):
                return VirtualPath(self.path.parent)

            def stat(self):
                return self.path.stat()

            def chmod(self, mode):
                return self.path.chmod(mode)

            def mkdir(self, *args, **kwargs):
                missing = []
                current = self.path
                while not current.exists():
                    missing.append(current)
                    current = current.parent
                result = self.path.mkdir(*args, **kwargs)
                for created in missing:
                    owner.owner[str(created)] = owner.uid
                return result

            def lstat(self):
                actual = self.path.lstat()
                return SimpleNamespace(st_uid=owner.owner.get(str(self), 0),
                    st_mode=actual.st_mode, st_nlink=actual.st_nlink,
                    st_size=actual.st_size, st_dev=actual.st_dev, st_ino=actual.st_ino)

            def read_bytes(self):
                info = self.lstat()
                if owner.uid not in (0, info.st_uid) and not info.st_mode & stat.S_IROTH:
                    raise PermissionError('virtual file owner mismatch')
                return self.path.read_bytes()

        def listdir(path):
            local = owner.local(path)
            info = local.stat()
            uid = owner.owner.get(str(local), 0)
            if owner.uid not in (0, uid) and not info.st_mode & stat.S_IXOTH:
                raise PermissionError('virtual directory owner mismatch')
            return original_listdir(local)

        def write(path, raw, mode):
            local = owner.local(path)
            original_write(local, raw, mode)
            owner.owner[str(local)] = owner.uid
            owner.writes.append((str(local.relative_to(owner.root)), mode, owner.uid))

        stack = ExitStack()
        stack.enter_context(patch.object(direct_runtime, 'Path', VirtualPath))
        stack.enter_context(patch.object(direct_runtime.os, 'listdir', side_effect=listdir))
        stack.enter_context(patch.object(direct_runtime.os, 'seteuid', side_effect=self.set_uid))
        stack.enter_context(patch.object(direct_runtime, '_write_new', side_effect=write))
        stack.enter_context(patch.object(direct_runtime, 'WorkerGitHub', return_value=self.client))
        stack.enter_context(patch.object(model_deployment, 'validate_chatgpt_auth'))
        return stack

    def stage(self):
        self.assertTrue(direct_runtime.stage_direct_workspace(self.plan, as_uid=self.set_uid))

    def test_stage_validate_roundtrip_keeps_real_modes_and_virtual_owners(self):
        with self.fixture():
            self.stage()
            self.assertEqual(self.uid, 0)
            direct_runtime.validate_direct_workspace(self.plan, self.manifest)
            self.assertEqual(self.uid, 0)
            direct_runtime.validate_worker_home(self.plan)
        expectations = {
            'work/goal': (0o755, 0),
            'work/goal/data': (0o755, 0),
            'work/goal/data/public.csv': (0o444, 0),
            'work/goal/output': (0o700, 10002),
            'work/goal/output/nested': (0o755, 10002),
            'work/goal/output/nested/editable.py': (0o600, 10002),
            'work/goal/reports': (0o755, 0),
            'work/goal/reports/research': (0o700, 10002),
            'work/worker-home': (0o700, 10002),
            'work/worker-home/worker-pr.json': (0o600, 10002),
            'work/worker-home/github-token': (0o600, 10002),
            'work/worker-home/approved-plan.json': (0o600, 10002),
            'work/codex-home/auth.json': (0o600, 10002),
        }
        for relative, (mode, uid) in expectations.items():
            with self.subTest(path=relative):
                path = self.root / relative
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                self.assertEqual(self.owner.get(str(path), 0), uid)
        for relative, raw in self.input_bytes.items():
            self.assertEqual((self.root / 'work/goal' / relative).read_bytes(), raw)
        self.assertEqual(os.geteuid(), self.real_euid)
        self.assertEqual(self.uid_events[-1], 0)
        self.client.request.assert_called_once_with('GET', '/user')

    def test_real_scope_permission_drift_rejected_with_euid_restored(self):
        with self.fixture():
            self.stage()
            (self.root / 'work/goal/output').chmod(0o755)
            with self.assertRaisesRegex(ContractError, 'directory permissions mismatch'):
                direct_runtime.validate_direct_workspace(self.plan, self.manifest)
            self.assertEqual(self.uid, 0)
            self.assertEqual(self.uid_events[-1], 0)

    def test_real_input_permission_drift_rejected_with_euid_restored(self):
        with self.fixture():
            self.stage()
            path = self.root / 'work/goal/output/nested/editable.py'
            path.chmod(0o644)
            with self.assertRaisesRegex(ContractError, 'input permissions mismatch'):
                direct_runtime.validate_direct_workspace(self.plan, self.manifest)
            self.assertEqual(self.uid, 0)

    def test_real_readonly_input_becoming_writable_rejected(self):
        with self.fixture():
            self.stage()
            (self.root / 'work/goal/data/public.csv').chmod(0o644)
            with self.assertRaisesRegex(ContractError, 'input permissions mismatch'):
                direct_runtime.validate_direct_workspace(self.plan, self.manifest)
            self.assertEqual(self.uid, 0)

    def test_changed_real_input_bytes_rejected_and_euid_restored(self):
        with self.fixture():
            self.stage()
            path = self.root / 'work/goal/output/nested/editable.py'
            raw = path.read_bytes()
            path.write_bytes(b'X' + raw[1:])
            with self.assertRaisesRegex(ContractError, 'input bytes changed'):
                direct_runtime.validate_direct_workspace(self.plan, self.manifest)
            self.assertEqual(self.uid, 0)

    def test_virtual_owner_drift_rejected_and_euid_restored(self):
        with self.fixture():
            self.stage()
            path = self.root / 'work/goal/output/nested/editable.py'
            self.owner[str(path)] = 10001
            with self.assertRaisesRegex(ContractError, 'ownership/mode mismatch'):
                direct_runtime.validate_direct_workspace(self.plan, self.manifest)
            self.assertEqual(self.uid, 0)

    def test_actual_owner_actor_rejected_before_real_worker_credential_files_exist(self):
        self.client.request.return_value = dict(id=OWNER_ID, login=OWNER_LOGIN)
        with self.fixture():
            with self.assertRaisesRegex(ContractError, 'actual worker credential identity mismatch'):
                self.stage()
            self.assertEqual(self.uid, 0)
        self.assertEqual(len(self.writes), len(self.input_bytes))
        self.assertEqual(list((self.root / 'work/worker-home').iterdir()), [])
        self.assertEqual(list((self.root / 'work/codex-home').iterdir()), [])
        self.assertEqual(os.geteuid(), self.real_euid)

    def test_input_stage_failure_restores_euid_without_staging_credentials(self):
        with self.fixture(), patch.object(direct_runtime, '_write_new', side_effect=OSError('offline disk fault')):
            with self.assertRaises(OSError):
                self.stage()
            self.assertEqual(self.uid, 0)
        self.client.request.assert_not_called()
        self.assertEqual(list((self.root / 'work/worker-home').iterdir()), [])
        self.assertEqual(os.geteuid(), self.real_euid)


class DirectPlanTemplateTests(unittest.TestCase):
    def test_direct_plan_template_contains_no_implicit_execution_or_publication_approval(self):
        output = io.StringIO()
        with patch.object(prepare.subprocess, 'run') as run, \
             patch.object(prepare.subprocess, 'Popen') as popen, redirect_stdout(output):
            prepare.main(['--direct-plan-template'])
        value = json.loads(output.getvalue())
        self.assertIs(value['publish_result_pr'], False)
        self.assertIs(value['model_usage_approved'], False)
        self.assertIs(value['direct_pr']['public_results_approved'], False)
        self.assertEqual(value['direct_pr']['issue_number'], 0)
        self.assertTrue(value['direct_pr']['title'].startswith('[Codex] '))
        run.assert_not_called()
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
