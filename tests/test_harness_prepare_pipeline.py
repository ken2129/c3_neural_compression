"""Run the real preparation shell with fake Docker, never a Docker daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_harness_deployment_contract import inspect_fixture,volume_fixture,SOURCE_SHA,CONTAINER_ID,VOLUME_NAME

FAKE = r'''#!/usr/bin/env python3
import io,json,os,pathlib,sys,tarfile
p=pathlib.Path(os.environ['FAKE_STATE']);a=sys.argv[1:]
with (p/'calls').open('a') as f:f.write(json.dumps(a)+'\n')
if a[0]=='compose':
 if 'create' in a:(p/'created').touch()
 elif 'ps' in a:print(os.environ['FAKE_ID'])
elif a[0]=='ps':
 if any(x.startswith('volume=') for x in a):print(os.environ['FAKE_ID'])
elif a[:2]==['volume','ls']:pass
elif a[:2]==['volume','inspect']:
 if not (p/'created').exists():sys.exit(1)
 print((p/'volume.json').read_text())
elif a[0]=='inspect':print((p/'inspect.json').read_text())
elif a[0]=='cp':
 if a[2]=='-':
  raw=sys.stdin.buffer.read()
  if os.environ.get('CORRUPT_OWNER'):
   with tarfile.open(fileobj=io.BytesIO(raw)) as t:
    entry=t.getmembers()[0];data=t.extractfile(entry).read()
   entry.uid=1000
   out=io.BytesIO()
   with tarfile.open(fileobj=out,mode='w') as t:t.addfile(entry,io.BytesIO(data))
   raw=out.getvalue()
  (p/'archive').write_bytes(raw)
 else:sys.stdout.buffer.write((p/'archive').read_bytes())
elif a[0]=='start':(p/'started').touch()
else:sys.exit('unexpected Docker operation')
'''

class PreparePipelineTests(unittest.TestCase):
 def run_pipeline(self,corrupt=False):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);repo=root/'repo';state=root/'state';bin=root/'bin'
   state.mkdir();bin.mkdir();(repo/'tools/research_service').mkdir(parents=True);(repo/'research_harness').mkdir()
   source=Path(__file__).resolve().parents[1]
   shutil.copy(source/'tools/research_service/prepare.sh',repo/'tools/research_service/prepare.sh')
   for name in ('__init__.py','schema.py','deployment_contract.py','deployment_archive.py'):
    shutil.copy(source/'research_harness'/name,repo/'research_harness'/name)
   (bin/'docker').write_text(FAKE);(bin/'docker').chmod(0o755)
   (bin/'git').write_text('#!/bin/sh\ncase "$*" in *rev-parse*) echo '+SOURCE_SHA+';; esac\n');(bin/'git').chmod(0o755)
   (state/'inspect.json').write_text(json.dumps([inspect_fixture()]))
   (state/'volume.json').write_text(json.dumps([volume_fixture()]))
   env=dict(os.environ,PATH=str(bin)+':'+os.environ['PATH'],FAKE_STATE=str(state),FAKE_ID=CONTAINER_ID,TMPDIR=str(root))
   if corrupt:env['CORRUPT_OWNER']='1'
   result=subprocess.run(['bash',str(repo/'tools/research_service/prepare.sh'),'--approve-dedicated-container','fixture-project',SOURCE_SHA],env=env,capture_output=True,text=True,timeout=10)
   calls=[json.loads(x) for x in (state/'calls').read_text().splitlines()]
   return result,(state/'started').exists(),calls
 def test_real_shell_places_reads_back_then_starts(self):
  result,started,calls=self.run_pipeline()
  self.assertEqual(result.returncode,0,result.stderr);self.assertTrue(started)
  cp=[i for i,a in enumerate(calls) if a[0]=='cp'];start=[i for i,a in enumerate(calls) if a[0]=='start']
  self.assertEqual(len(cp),2);self.assertLess(cp[-1],start[0])
 def test_wrong_readback_owner_never_starts(self):
  result,started,calls=self.run_pipeline(True)
  self.assertNotEqual(result.returncode,0);self.assertFalse(started)
  self.assertFalse(any(a[0]=='start' for a in calls))
