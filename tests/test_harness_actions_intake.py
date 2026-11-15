"""Offline contract tests; never start a runner, manager, or model."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("issue_event", ROOT / "tools/research_actions/issue_event.py")
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.event = {"repository": {"full_name": client.REPOSITORY, "id": client.REPOSITORY_ID},
                      "sender": {"login": client.OWNER, "id": client.OWNER_ID},
                      "issue": {"number": 92, "body": "$(touch /tmp/never)"}, "action": "opened"}
        self.env = {"GITHUB_REPOSITORY": client.REPOSITORY,
                    "GITHUB_REPOSITORY_ID": str(client.REPOSITORY_ID),
                    "GITHUB_REF": "refs/heads/main", "GITHUB_ACTOR": client.OWNER,
                    "GITHUB_ACTOR_ID": str(client.OWNER_ID), "C3_TRIGGERING_ACTOR": client.OWNER,
                    "GITHUB_EVENT_NAME": "issues", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_WORKFLOW_SHA": "a" * 40}

    def test_issue_receipt_has_no_body(self):
        value = client.receipt(self.event, self.env)
        self.assertEqual(value["issue_number"], 92)
        self.assertNotIn("body", value)
        self.assertNotIn("touch", json.dumps(value))

    def test_comment_actions(self):
        self.env["GITHUB_EVENT_NAME"] = "issue_comment"
        self.event["comment"] = {"id": 44, "body": "do not execute me"}
        for action in ("created", "edited", "deleted"):
            self.event["action"] = action
            self.assertEqual(client.receipt(self.event, self.env)["comment_id"], 44)

    def test_issue_actions(self):
        for action in client.ACTIONS["issues"]:
            self.event["action"] = action
            self.assertEqual(client.receipt(self.event, self.env)["action"], action)

    def test_pr_comment_rejected(self):
        self.event["issue"]["pull_request"] = {}
        with self.assertRaises(client.Rejected):
            client.receipt(self.event, self.env)

    def test_context_mismatches_rejected(self):
        for field in ("GITHUB_REPOSITORY", "GITHUB_REPOSITORY_ID", "GITHUB_REF", "GITHUB_ACTOR",
                      "GITHUB_ACTOR_ID", "C3_TRIGGERING_ACTOR", "GITHUB_EVENT_NAME", "GITHUB_WORKFLOW_SHA"):
            with self.subTest(field=field):
                env = dict(self.env, **{field: "untrusted"})
                with self.assertRaises(client.Rejected):
                    client.receipt(self.event, env)

    def test_sender_and_repository_mismatches(self):
        for section, field in (("sender", "id"), ("sender", "login"), ("repository", "id"), ("repository", "full_name")):
            event = copy.deepcopy(self.event)
            event[section][field] = "untrusted"
            with self.assertRaises(client.Rejected):
                client.receipt(event, self.env)

    def test_invalid_numbers(self):
        for value in (True, 0, -1, "92", 2**63):
            self.event["issue"]["number"] = value
            with self.assertRaises(client.Rejected):
                client.receipt(self.event, self.env)

    def test_invalid_run_ids(self):
        for value in ("0", "-1", "1; echo bad", "01", "9" * 20, None):
            self.env["GITHUB_RUN_ID"] = value
            with self.assertRaises(client.Rejected):
                client.receipt(self.event, self.env)

    def test_rerun_same_receipt_later_event_distinct(self):
        first = client.receipt(self.event, self.env)
        self.env["GITHUB_RUN_ATTEMPT"] = "2"
        self.assertEqual(first["receipt_id"], client.receipt(self.event, self.env)["receipt_id"])
        self.env["GITHUB_RUN_ID"] = "124"
        self.assertNotEqual(first["receipt_id"], client.receipt(self.event, self.env)["receipt_id"])

    def test_delivery_fixed_argv_no_shell_or_credentials(self):
        item = client.receipt(self.event, self.env)
        def run(argv, **kwargs):
            self.assertEqual(argv, ["/opt/c3/bin/submit-issue-event", "--receipt-stdin"])
            self.assertNotIn("shell", kwargs)
            self.assertEqual(json.loads(kwargs["input"]), item)
            self.assertEqual(set(kwargs["env"]), {"PATH", "LANG"})
            return subprocess.CompletedProcess(argv, 0, json.dumps({"schema": "c3-issue-event-ack-v1", "receipt_id": item["receipt_id"], "status": "enqueued"}))
        self.assertEqual(client.deliver(item, run), "enqueued")

    def test_invalid_or_failed_ack_rejected(self):
        item = client.receipt(self.event, self.env)
        valid = {"schema": "c3-issue-event-ack-v1", "receipt_id": item["receipt_id"], "status": "duplicate"}
        for code, ack in [(1, valid), (0, {}), (0, dict(valid, status="completed")),
                          (0, dict(valid, receipt_id="another")), (0, dict(valid, schema="other"))]:
            with self.subTest(code=code, ack=ack):
                with self.assertRaises(client.Rejected):
                    client.deliver(item, lambda *a, **kw: subprocess.CompletedProcess(a, code, json.dumps(ack)))
        self.assertEqual(client.deliver(item, lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(valid))), "duplicate")

    def test_timeout_not_retried(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(1)
            raise subprocess.TimeoutExpired(args[0], 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            client.deliver(client.receipt(self.event, self.env), run)
        self.assertEqual(len(calls), 1)

    def test_workflow_off_gates_and_no_checkout(self):
        text = (ROOT / ".github/workflows/research-issue-intake.yml").read_text()
        for required in ("vars.C3_ISSUE_INTAKE_ENABLED == 'true'", "vars.C3_ISSUE_INTAKE_CONTRACT == 'c3-issue-event-v1'",
                         "github.actor_id == '55587773'", "github.repository_id == '1312915009'",
                         "!github.event.issue.pull_request", "permissions: {}", "timeout-minutes: 2"):
            self.assertIn(required, text)
        self.assertNotIn("uses:", text)
        self.assertNotIn("schedule:", text)
        self.assertNotIn("workflow_dispatch:", text)
        self.assertNotIn("concurrency:", text)
        self.assertNotIn("github.event.issue.body", text)
        self.assertNotIn("github.event.comment.body", text)
        self.assertIn("/usr/bin/python3 -I /opt/c3/actions/issue_event.py", text)


if __name__ == "__main__":
    unittest.main()
