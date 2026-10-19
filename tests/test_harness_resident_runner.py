"""Model-free filesystem/exec regression; real root->10003 still needs Docker."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_harness import resident_runner
from research_harness.schema import ContractError

ROOT=Path(__file__).resolve().parents[1]


class RunnerBootstrapTests(unittest.TestCase):
    def fixture(self,root):
        root.chmod(0o700)
        (root/'bin').mkdir()
        binary=root/'bin/Runner.Listener'
        binary.write_text('#!'+sys.executable+'\nimport os,json,sys\n'
            'print(json.dumps(dict(cwd=os.getcwd(),pid=os.getpid(),uid=os.geteuid(),args=sys.argv[1:])))\n')
        binary.chmod(0o555)
        return binary

    def launch(self,root):
        code=('import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
              'from research_harness.resident_runner import exec_private_runner;'
              'exec_private_runner(Path(sys.argv[2]))')
        return subprocess.Popen([sys.executable,'-I','-B','-c',code,str(ROOT),str(root)],
            cwd='/',stdout=subprocess.PIPE,stderr=subprocess.PIPE)

    def test_actual_exec_enters_owned_private_directory_and_preserves_child_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            child=self.launch(root);out,err=child.communicate(timeout=5)
            self.assertEqual((child.returncode,err),(0,b''))
            value=json.loads(out)
            self.assertEqual(value,dict(cwd=str(root.resolve()),pid=child.pid,uid=os.geteuid(),
                args=['run','--startuptype','service']))
            self.assertEqual(root.stat().st_mode&0o777,0o700)

    @unittest.skipIf(os.geteuid()==0,'DAC denial requires an unprivileged test process')
    def test_real_popen_reports_chdir_denial_before_child_bootstrap_even_with_user_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0)
            try:
                with self.assertRaises(PermissionError) as caught:
                    subprocess.Popen([sys.executable,'-I','-B','-c','raise SystemExit(99)'],
                        user=os.getuid(),group=os.getgid(),cwd=str(root))
                self.assertEqual(caught.exception.errno,errno.EACCES)
                self.assertEqual(caught.exception.filename,str(root))
            finally:root.chmod(0o700)  # Test-owned temporary directory only.

    def test_actual_filesystem_rejects_nonprivate_directory_without_executing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root);root.chmod(0o755)
            with self.assertRaises(ContractError):resident_runner.exec_private_runner(root)
            self.assertEqual(root.stat().st_mode&0o777,0o755)

    def test_actual_filesystem_rejects_link_and_writable_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);binary=self.fixture(root)
            binary.chmod(0o775)
            with self.assertRaises(ContractError):resident_runner.exec_private_runner(root)
            binary.unlink();binary.symlink_to(sys.executable)
            with self.assertRaises(ContractError):resident_runner.exec_private_runner(root)

    def test_identity_requires_permanent_runner_uid_gid_no_groups_no_caps_and_nnp(self):
        zero='CapEff: 0\nCapPrm: 0\nCapInh: 0\nCapAmb: 0\nNoNewPrivs: 1\n'
        # Only identity facts are simulated; actual filesystem/exec is above.
        with patch.object(resident_runner.os,'getresuid',return_value=(10003,)*3,create=True), \
             patch.object(resident_runner.os,'getresgid',return_value=(10003,)*3,create=True), \
             patch.object(resident_runner.os,'getgroups',return_value=[]), \
             patch.object(resident_runner.Path,'read_text',return_value=zero):
            resident_runner.require_runner_identity()
            for text in (zero.replace('CapEff: 0','CapEff: 1'),zero.replace('NoNewPrivs: 1','NoNewPrivs: 0')):
                with patch.object(resident_runner.Path,'read_text',return_value=text),self.assertRaises(ContractError):
                    resident_runner.require_runner_identity()
            with patch.object(resident_runner.os,'getresuid',return_value=(0,10003,0)),self.assertRaises(ContractError):
                resident_runner.require_runner_identity()
            with patch.object(resident_runner.os,'getgroups',return_value=[10003]),self.assertRaises(ContractError):
                resident_runner.require_runner_identity()


if __name__=='__main__':unittest.main()
