"""Control-flow tests with a fake Docker executable; never contact Docker."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

RUNNER = Path(__file__).with_name('run_checks.sh')
FAKE = '''#!/usr/bin/env python3
import os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f: f.write(' '.join(args)+'\\n')
if args[:2] == ['context','inspect']: print('unix:///var/run/docker.sock')
elif args[0] == 'info': print(os.environ['TMPDIR'])
elif args[:2] == ['image','inspect']: print('sha256:fake-image')
elif args[0] == 'ps': sys.exit(int(os.environ.get('PS_EXIT','0')))
elif args[0] == 'compose' and 'run' in args:
 if 'compose.seccomp-candidate.yaml' in args:
  print('sandbox_true_exit=0');sys.exit(0)
 scenario=os.environ['SCENARIO']
 if scenario=='ok': print('sandbox_true_exit=0');sys.exit(0)
 if scenario=='timeout': print('sandbox_true_exit=124');sys.exit(124)
 if scenario=='namespace':
  print('bwrap: No permissions to create a new namespace');print('sandbox_true_exit=1');sys.exit(1)
 sys.exit(1)
'''

class RunnerTests(unittest.TestCase):
    def check(self, scenario, flag=True, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'docker').write_text(FAKE)
            (root/'docker').chmod(0o755)
            (root/'df').write_text('#!/bin/sh\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted\\nfake 9999999 1 9999998 1%% /\\n"\n')
            (root/'df').chmod(0o755)
            env = dict(os.environ, PATH=tmp+os.pathsep+os.environ['PATH'], TMPDIR=tmp, CALLS=str(root/'calls'), SCENARIO=scenario)
            env.pop('DOCKER_HOST',None);env.pop('DOCKER_CONTEXT',None)
            env.update(extra)
            proc = subprocess.run(['bash',str(RUNNER)]+(['--allow-seccomp-candidate'] if flag else []),env=env,capture_output=True,text=True,timeout=10)
            calls=(root/'calls').read_text()
            candidate=any('run ' in line and 'compose.seccomp-candidate.yaml' in line for line in calls.splitlines())
            return proc.returncode,candidate,calls

    def test_baseline_success_avoids_candidate(self):
        code,candidate,_=self.check('ok');self.assertEqual(code,0);self.assertFalse(candidate)
    def test_namespace_failure_uses_approved_candidate(self):
        code,candidate,_=self.check('namespace');self.assertEqual(code,0);self.assertTrue(candidate)
    def test_no_opt_in_avoids_candidate(self):
        code,candidate,_=self.check('namespace',False);self.assertEqual(code,1);self.assertFalse(candidate)
    def test_timeout_avoids_candidate(self):
        code,candidate,_=self.check('timeout');self.assertEqual(code,124);self.assertFalse(candidate)
    def test_missing_marker_avoids_candidate(self):
        code,candidate,_=self.check('missing');self.assertEqual(code,1);self.assertFalse(candidate)
    def test_failed_listing_stops_before_build(self):
        code,candidate,calls=self.check('namespace',PS_EXIT='1');self.assertNotEqual(code,0);self.assertFalse(candidate);self.assertNotIn('build sandbox-check',calls)
    def test_remote_override_stops_before_build(self):
        code,candidate,calls=self.check('namespace',DOCKER_HOST='ssh://example.invalid');self.assertNotEqual(code,0);self.assertFalse(candidate);self.assertNotIn('build sandbox-check',calls)

if __name__=='__main__': unittest.main()
