import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research_harness.schema import (Plan, ContractError, contained_path, fixture_plan,
                                     freeze_source, loads, new_salt, require_live_environment)


class SchemaTest(unittest.TestCase):
    def test_roundtrip_private_digest(self):
        plan = fixture_plan()
        self.assertEqual(Plan.parse(loads(json.dumps(plan.to_dict()))), plan)
        self.assertNotEqual(plan.digest(new_salt()), plan.digest(new_salt()))

    def test_unknown_types_and_nonfinite(self):
        for key, value in [('runtime_environment', {'PYTHONPATH': '/tmp/evil'}),
                           ('command', ['sh', '-c', 'true']), ('version', True),
                           ('revision', False), ('storage_required', 'true'),
                           ('operation', 'codex'), ('environment', 'shared-machine'),
                           ('source_sha', 'main'), ('repo', 'other/repo')]:
            with self.subTest(key=key):
                data = fixture_plan().to_dict(); data[key] = value
                with self.assertRaises(ContractError): Plan.parse(data)
        for value in [float('nan'), float('inf'), -1, True, '5']:
            data = fixture_plan().to_dict(); data['budget']['wall_seconds'] = value
            with self.assertRaises(ContractError): Plan.parse(data)
        for text in ['{"x": 1, "x": 2}', '{"x": NaN}', '{"x": Infinity}']:
            with self.assertRaises(ContractError): loads(text)

    def test_paths_and_symlinks(self):
        for path in ['../secret', '/tmp/x', 'a/../b', 'a//b', './x', 'x\\y']:
            data = fixture_plan().to_dict(); data['allowed_paths'] = [path]
            with self.assertRaises(ContractError): Plan.parse(data)
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'link').symlink_to('/tmp')
            with self.assertRaises(ContractError): contained_path(root, 'link/file')
            self.assertEqual(contained_path(root, 'safe/file'), Path(root) / 'safe/file')

    def test_source_scope_two_stages(self):
        plan = fixture_plan()
        with self.assertRaises(ContractError): freeze_source(plan, '1' * 40, list(plan.allowed_paths))
        with self.assertRaises(ContractError):
            freeze_source(plan, '1' * 40, ['research_harness/schema.py'], independently_validated=True)
        with self.assertRaises(ContractError):
            freeze_source(plan, '1' * 40, list(plan.allowed_paths), independently_validated='yes')
        frozen = freeze_source(plan, '1' * 40, list(plan.allowed_paths), independently_validated=True)
        self.assertEqual(frozen['base_sha'], plan.source_sha)
        self.assertEqual(frozen['source_sha'], '1' * 40)

    def test_live_always_closed_and_no_ml_import(self):
        with self.assertRaisesRegex(ContractError, 'BLOCKED_ENV'): require_live_environment()
        code = ('import sys; import research_harness.schema; '
                'assert not any(x in sys.modules for x in ["torch", "jax", "numpy"])')
        subprocess.run([sys.executable, '-c', code], check=True)

    def test_optimized_python_keeps_gates(self):
        code = ('from research_harness.schema import require_live_environment; '
                'require_live_environment()')
        result = subprocess.run([sys.executable, '-O', '-c', code], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'BLOCKED_ENV', result.stderr)
