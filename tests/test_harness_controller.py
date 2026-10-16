import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_harness.controller import capabilities, demo


class ControllerTest(unittest.TestCase):
    def test_complete_roundtrip_and_separate_statuses(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'fresh'
            result = demo(directory)
            self.assertEqual(result['state'], 'COMPLETE')
            self.assertEqual(result['science_status'], 'COMPLETE')
            self.assertEqual(result['report_status'], 'DELIVERED')
            self.assertEqual(result['fake_merge_calls'], 1)
            self.assertTrue(result['checksum_verified'])
            self.assertTrue(result['public_result']['fixture_only'])
            self.assertEqual(result['public_result']['outcome'], 'negative')
            self.assertEqual(result['live_execution'], 'BLOCKED_ENV')
            self.assertEqual(json.loads((directory / 'publication.json').read_text())['phase'], 'MERGED')
            with self.assertRaises(FileExistsError): demo(directory)

    def test_launch_failure_does_not_validate_or_publish(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('research_harness.controller.ProcessManager.launch', side_effect=OSError('fixture fault')):
                with self.assertRaises(OSError): demo(Path(root) / 'fresh')
            self.assertFalse((Path(root) / 'fresh' / 'validation.json').exists())
            self.assertFalse((Path(root) / 'fresh' / 'publication.json').exists())

    def test_disk_preflight_prevents_process_launch(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            with patch('research_harness.controller.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                with patch('research_harness.controller.ProcessManager.launch') as launch:
                    with self.assertRaisesRegex(ValueError, 'BLOCKED_RESOURCE'):
                        demo(Path(root) / 'fresh')
                    launch.assert_not_called()

    def test_no_live_switch_or_heavy_import(self):
        self.assertEqual(capabilities()['live_execution'], 'BLOCKED_ENV')
        result = subprocess.run([sys.executable, '-m', 'research_harness', 'run'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        code = ('import sys; from research_harness.controller import capabilities; '
                'assert capabilities()["live_execution"] == "BLOCKED_ENV"; '
                'assert not any(x in sys.modules for x in ["torch","jax","numpy"])')
        subprocess.run([sys.executable, '-c', code], check=True)
