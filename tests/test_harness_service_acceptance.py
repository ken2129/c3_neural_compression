"""Offline shell acceptance checks use a fake docker executable only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'tools/container_boundary/check_service.sh'

class AcceptanceTests(unittest.TestCase):
    def run_script(self,bad=False):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);binary=root/'docker'
            binary.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
args=sys.argv
if 'run' not in args:sys.exit(0)
base={'fixture_only':True,'execution_allowed':False}
if args[-1]=='initialize':base['initialized']=True
else:
 p=pathlib.Path(os.environ['FAKE_COUNTER']);n=int(p.read_text()) if p.exists() else 0;p.write_text(str(n+1))
 base.update(phase='COMPLETED',dispatched=(n==0 or os.environ['BAD']=='1'),duplicate=n>0,result_saved=True)
print(json.dumps(base))
''')
            binary.chmod(0o700)
            env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],TMPDIR=temp,
                     FAKE_COUNTER=str(root/'count'),BAD='1' if bad else '0')
            return subprocess.run(['bash',str(SCRIPT),'--allow-new-diagnostic-volume'],env=env,capture_output=True,text=True,timeout=10)

    def test_expected_persisted_reopen_passes(self):
        result=self.run_script()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('persistent_recreate_checks_passed=true',result.stdout)

    def test_duplicate_dispatch_is_failure_even_with_exit_zero(self):
        result=self.run_script(True)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('persistent_recreate_checks_passed=true',result.stdout)

    def test_no_implicit_new_volume(self):
        result=subprocess.run(['bash',str(SCRIPT)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
