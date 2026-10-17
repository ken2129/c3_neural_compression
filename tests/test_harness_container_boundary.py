"""Unit checks only; Docker acceptance must run separately on target host."""
import errno
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('boundary_probe', ROOT / 'tools/container_boundary/probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class BoundaryContractTests(unittest.TestCase):
    def identity(self):
        return {'Uid': '10002\t10002\t10002\t10002', 'Gid': '10002\t10002\t10002\t10002',
                'Groups': '', 'CapEff': '0', 'CapPrm': '0', 'CapAmb': '0', 'NoNewPrivs': '1'}

    def test_exact_identity(self):
        self.assertTrue(probe.identity_ok(self.identity(), 10002))
        for key, value in [('Uid', '10002 0 10002 10002'), ('Gid', '0 0 0 0'), ('Groups', '10001'),
                           ('CapEff', '1'), ('CapPrm', '1'), ('CapAmb', '1'), ('NoNewPrivs', '0')]:
            with self.subTest(key=key):
                values = self.identity(); values[key] = value
                self.assertFalse(probe.identity_ok(values, 10002))

    def test_only_permission_errors_prove_denial(self):
        for code in (errno.EPERM, errno.EACCES, errno.ENOENT, errno.EIO):
            def operation():
                raise OSError(code, 'synthetic error')
            self.assertEqual(probe.denied(operation), code in (errno.EPERM, errno.EACCES))
        self.assertFalse(probe.denied(lambda: None))

    def test_compose_has_no_host_access(self):
        config = (ROOT / 'tools/container_boundary/compose.yaml').read_text()
        for required in ('context: .', 'dockerfile: Dockerfile', 'network_mode: none', 'read_only: true', 'cap_drop: [ALL]',
                         'cap_add: [SETUID, SETGID]', 'no-new-privileges:true', 'pids_limit: 16',
                         'uid=10001,gid=10001', 'uid=10002,gid=10002'):
            self.assertIn(required, config)
        for forbidden in ('privileged:', 'volumes:', 'devices:', 'SYS_ADMIN', 'docker.sock', 'pid: host'):
            self.assertNotIn(forbidden, config)
