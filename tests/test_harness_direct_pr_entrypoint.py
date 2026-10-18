"""Offline entrypoint policy: no actual namespace, credentials or model."""
import io
import json
import signal
import unittest
from unittest.mock import patch

from tests.test_harness_goal_deployment import launcher, prepare
from research_harness.goal_plan import validate_goal_plan
from research_harness.schema import ContractError


class DirectEntrypointTests(unittest.TestCase):
    def test_planning_template_never_authorizes_execution(self):
        output = io.StringIO()
        with patch('sys.stdout', output), patch.object(prepare.subprocess, 'run') as run:
            prepare.main(['--direct-plan-template'])
        run.assert_not_called()
        value = json.loads(output.getvalue())
        self.assertFalse(value['model_usage_approved'])
        self.assertFalse(value['publish_result_pr'])
        self.assertFalse(value['direct_pr']['public_results_approved'])
        with self.assertRaises(ContractError):
            validate_goal_plan(value, 'a'*40)

    def test_namespace_shutdown_handlers_exit_only_own_pid1(self):
        with patch.object(launcher.os, 'getpid', return_value=1), \
                patch.object(launcher.os, 'getresuid', return_value=(0, 0, 0)), \
                patch.object(launcher.signal, 'signal') as register, \
                patch.object(launcher.os, '_exit') as exit_own:
            launcher.install_pid1_shutdown_handlers()
            calls = {call.args[0]: call.args[1] for call in register.call_args_list}
            self.assertEqual(set(calls), {signal.SIGTERM, signal.SIGINT})
            calls[signal.SIGTERM](signal.SIGTERM, None)
            exit_own.assert_called_with(143)
            calls[signal.SIGINT](signal.SIGINT, None)
            exit_own.assert_called_with(130)
        for pid, uids in ((22, (0, 0, 0)), (1, (10001,)*3)):
            with patch.object(launcher.os, 'getpid', return_value=pid), \
                    patch.object(launcher.os, 'getresuid', return_value=uids), \
                    patch.object(launcher.signal, 'signal') as register:
                with self.assertRaises(ContractError):
                    launcher.install_pid1_shutdown_handlers()
                register.assert_not_called()
