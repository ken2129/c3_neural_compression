"""Fresh six-job sequence; a private approved plan and validated assets are required.

The measured repair continuation is preserved separately in resume_sequence.py.
"""
import fcntl
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import time
from supervise import run


def main():
    root = Path(__file__).resolve().parents[1]
    pilot = root / "pilot"
    plan = json.loads((pilot / "execution_plan.json").read_text())
    approval = json.loads((pilot / "approval.json").read_text())
    if approval.get("approved") is not True or approval.get("plan_sha256") != hashlib.sha256((pilot / "execution_plan.json").read_bytes()).hexdigest():
        raise ValueError("This exact GPU pilot scope has not been approved")
    os.sched_setaffinity(0, plan["CPU_affinity"])
    for package, version in json.loads((root / "cpu_preflight.json").read_text())["versions"].items():
        if metadata.version(package) != version:
            raise ValueError("Dependency version changed")
    with (root / "control/sequence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if list((root / "control").glob("*_receipt.json")):
            raise ValueError("Existing attempts; do not retry or overwrite")
        os.environ["C3_PILOT_DEADLINE"] = str(time.monotonic() + 7200)
        for name in ("smoke_image", "smoke_combined", "A", "C", "B", "D"):
            if time.monotonic() >= float(os.environ["C3_PILOT_DEADLINE"]):
                raise RuntimeError("Overall wall ceiling reached")
            run(pilot / "jobs" / (name + ".json"))


if __name__ == "__main__":
    main()
