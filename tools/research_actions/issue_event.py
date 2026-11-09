"""Installed, trusted Actions receipt client. No checkout or issue text execution.

Machine-side /opt/c3/bin/submit-issue-event is a NEW Issue92 contract, not an
existing service command. See docs/research_actions_intake.md before enabling.
"""
import json
import os
import re
import subprocess
import sys

REPOSITORY = "ken2129/c3_neural_compression"
REPOSITORY_ID = 1312915009
OWNER = "ken2129"
OWNER_ID = 55587773
CONTRACT = "c3-issue-event-v1"
SUBMIT = "/opt/c3/bin/submit-issue-event"
ACTIONS = {
    "issues": {"opened", "edited", "reopened", "closed", "labeled", "unlabeled"},
    "issue_comment": {"created", "edited", "deleted"},
}
MAX_EVENT_BYTES = 1024 * 1024


class Rejected(ValueError):
    pass


def require(condition):
    if not condition:
        raise Rejected("EVENT_REJECTED")


def number(value):
    require(type(value) is int and 0 < value < 2**63)
    return value


def decimal(value):
    require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value))
    return number(int(value))


def receipt(event, env):
    require(isinstance(event, dict))
    repo = event.get("repository", {})
    sender = event.get("sender", {})
    issue = event.get("issue", {})
    require(all(isinstance(x, dict) for x in (repo, sender, issue)))
    require(repo.get("full_name") == REPOSITORY and repo.get("id") == REPOSITORY_ID)
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY)
    require(env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID))
    require(env.get("GITHUB_REF") == "refs/heads/main")
    require(env.get("GITHUB_ACTOR") == OWNER and env.get("GITHUB_ACTOR_ID") == str(OWNER_ID))
    require(env.get("C3_TRIGGERING_ACTOR") == OWNER)
    require(sender.get("login") == OWNER and sender.get("id") == OWNER_ID)
    require("pull_request" not in issue)
    name = env.get("GITHUB_EVENT_NAME")
    action = event.get("action")
    require(name in ACTIONS and action in ACTIONS[name])
    issue_number = number(issue.get("number"))
    run_id = decimal(env.get("GITHUB_RUN_ID"))
    attempt = decimal(env.get("GITHUB_RUN_ATTEMPT"))
    workflow_sha = env.get("GITHUB_WORKFLOW_SHA", "")
    require(isinstance(workflow_sha, str) and re.fullmatch(r"[0-9a-f]{40}", workflow_sha))
    comment_id = None
    if name == "issue_comment":
        comment = event.get("comment")
        require(isinstance(comment, dict))
        comment_id = number(comment.get("id"))
    return {
        "schema": CONTRACT,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "issue_number": issue_number,
        "event_name": name,
        "action": action,
        "actor_id": OWNER_ID,
        "comment_id": comment_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "workflow_sha": workflow_sha,
        # Attempts of the same Actions run are the same delivery. A later
        # approval event has a different run_id and MUST be considered again.
        "receipt_id": f"actions:{REPOSITORY_ID}:{run_id}",
    }


def deliver(item, run=subprocess.run):
    response = run(
        [SUBMIT, "--receipt-stdin"],
        input=json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n",
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=30, check=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    require(response.returncode == 0 and len(response.stdout) <= 4096)
    ack = json.loads(response.stdout)
    require(isinstance(ack, dict))
    require(ack.get("schema") == "c3-issue-event-ack-v1")
    require(ack.get("receipt_id") == item["receipt_id"])
    require(ack.get("status") in {"enqueued", "duplicate"})
    return ack["status"]


def main():
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], "rb") as handle:
            raw = handle.read(MAX_EVENT_BYTES + 1)
        require(len(raw) <= MAX_EVENT_BYTES)
        item = receipt(json.loads(raw), os.environ)
        status = deliver(item)
    except (KeyError, ValueError, TypeError, OSError, subprocess.SubprocessError):
        # Never echo event bodies, API errors, credentials or manager stderr.
        print("ISSUE_EVENT_DELIVERY_FAILED", file=sys.stderr)
        return 1
    print("ISSUE_EVENT_" + status.upper())
    return 0


if __name__ == "__main__":
    sys.exit(main())
