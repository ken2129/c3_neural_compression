"""Exercise fixed CLI wiring with fake identities/clients only; no Docker or gh."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('publication_entrypoint',Path(__file__).resolve().parents[1]/'tools/research_service/recover_publication.py')
entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)

class RecoveryEntrypointTests(unittest.TestCase):
    def test_explicit_modes_pass_to_fixed_publication_only_function(self):
        for mode in ('prepare','publish'):
            fence={'mode':mode,'recovery':{'Id':'2'*64}}
            def path(value):
                return SimpleNamespace(read_text=lambda: '2'*12 if value=='/etc/hostname' else 'a'*40)
            with self.subTest(mode=mode),patch.object(sys,'argv',['recover_publication.py','--once']), \
                 patch.object(entry.os,'getresuid',return_value=(10001,)*3), \
                 patch.object(entry.os,'getresgid',return_value=(10001,)*3), \
                 patch.object(entry.os,'getgroups',return_value=[10001]), \
                 patch.object(entry.os,'umask'),patch.dict(os.environ,{'GH_TOKEN':'never-inherit'},clear=True), \
                 patch.object(entry,'Path',side_effect=path), \
                 patch.object(entry,'read_protected_fence',return_value=fence), \
                 patch.object(entry,'recover_publication',return_value={'phase':'FAKE'}) as recover, \
                 patch.object(entry,'GhResultTransport',return_value=object()), \
                 patch.object(entry,'GitHubOwnerComments',return_value=object()), \
                 patch('builtins.print'):
                entry.main()
                self.assertEqual(recover.call_args.args,('/control/state','/control/publication-recovery'))
                self.assertIs(recover.call_args.kwargs['publish'],mode=='publish')
                self.assertNotIn('GH_TOKEN',os.environ)
                self.assertEqual(os.environ['GH_CONFIG_DIR'],'/control/gh')

    def test_command_line_cannot_enable_publish_or_model(self):
        for option in ('--publish','--model','--run'):
            with self.subTest(option=option),patch.object(sys,'argv',['recover_publication.py',option]), \
                 patch.object(entry,'recover_publication') as recover, \
                 patch.object(sys,'stderr'),self.assertRaises(SystemExit):
                entry.main()
            recover.assert_not_called()
