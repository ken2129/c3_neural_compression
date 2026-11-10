"""CPU-only authorization and process-group cleanup checks; GPU CLI is mocked."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parent / "runtime"
spec = importlib.util.spec_from_file_location("pilot_supervise", RUNTIME / "supervise.py")
supervise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervise)


def write(path, value):
    path.write_text(json.dumps(value))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)

    def test_unapproved_job_cannot_create_output_or_call_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / "plan.json", {})
            write(root / "approval.json", {"approved": False})
            write(root / "job.json", {"scope_plan_file": str(root / "plan.json"),
                                     "scope_approval_file": str(root / "approval.json")})
            with patch.object(supervise.subprocess, "check_output") as query:
                with self.assertRaisesRegex(ValueError, "Approval"):
                    supervise.run(root / "job.json")
                query.assert_not_called()
            self.assertEqual(len(list(root.iterdir())), 3)

    def test_fresh_sequence_refuses_unapproved_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pilot").mkdir()
            for name in ("sequence.py", "supervise.py"):
                (root / "pilot" / name).write_bytes((RUNTIME / name).read_bytes())
            write(root / "pilot/execution_plan.json", {})
            write(root / "pilot/approval.json", {"approved": False})
            result = subprocess.run([sys.executable, "-B", str(root / "pilot/sequence.py")],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("has not been approved", result.stderr)
            self.assertFalse((root / "control").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux procfs and affinity required")
    def test_timeout_cleans_worker_and_child_but_preserves_unrelated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("control", "runs", "results", "evidence", "cache"):
                (root / name).mkdir()
            worker = root / "worker.py"
            worker.write_text(
                "import os,sys,json,time,subprocess\nfrom pathlib import Path\n"
                "assert set(os.environ['JAX_PLATFORMS'].split(','))=={'cuda','cpu'}\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                + f"Path({str(root / 'pids.json')!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                + "time.sleep(60)\n")
            write(root / "manifest.json", {"image_ids": [1]})
            write(root / "subset.json", [1])
            plan = dict(private_root=str(root), control_dir=str(root / "control"), GPU_indices=[0],
                        GPU_UUIDs={"0": "MOCK_GPU"}, permitted_job_ids=["timeout"],
                        job_registry=str(root / "registry.json"), runtime_root=str(root), runtime_commit="SOURCE",
                        interpreter=sys.executable, runner=str(worker), max_GPU_seconds=10,
                        max_RAM_bytes=1024**3, disk_reserve_bytes=0, max_new_artifact_bytes=1024**2,
                        runtime_storage_roots=[str(root / n) for n in ("control", "runs", "results", "evidence", "cache")],
                        runtime_storage_baseline_bytes=0, asset_sha256={},
                        execution_source_sha256={str(worker): digest(worker)}, runtime_environment={})
            write(root / "plan.json", plan)
            job = dict(job_id="timeout", mode="codec", scope_plan_file=str(root / "plan.json"),
                       scope_approval_file=str(root / "approval.json"), GPU=0,
                       manifest=str(root / "manifest.json"), manifest_sha256=digest(root / "manifest.json"),
                       subset_ids_file=str(root / "subset.json"), image_root=str(root),
                       annotation_file=str(root / "manifest.json"), job_dir=str(root / "runs/timeout"),
                       output_dir=str(root / "results/timeout"), evidence_dir=str(root / "evidence/timeout"),
                       max_gpu_seconds=0.2, max_local_growth_bytes=1024**2)
            write(root / "job.json", job)
            write(root / "registry.json", {"scope_plan_sha256": digest(root / "plan.json"),
                                          "job_sha256": {"timeout": digest(root / "job.json")}})
            write(root / "approval.json", {"approved": True, "plan_sha256": digest(root / "plan.json")})

            def query(args, **kwargs):
                if args[0] == "git":
                    return "SOURCE\n" if "rev-parse" in args else ""
                self.assertEqual(args[0], "nvidia-smi")
                if "--query-gpu=index,uuid" in args:
                    return "0, MOCK_GPU\n"
                if "--query-gpu=utilization.gpu,memory.used" in args:
                    return "0, 0\n"
                if "--query-gpu=memory.used" in args:
                    return "0\n"
                if "--query-compute-apps=gpu_uuid,pid" in args:
                    return ""
                raise AssertionError("Unexpected query")

            sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
            try:
                with patch.object(supervise.subprocess, "check_output", side_effect=query), \
                        patch.dict(os.environ, {"C3_PILOT_DEADLINE": "inf"}):
                    with self.assertRaisesRegex(RuntimeError, "failed or stopped"):
                        supervise.run(root / "job.json")
                receipt = json.loads((root / "control/timeout_receipt.json").read_text())
                self.assertEqual(receipt["stop_reason"], "job_GPU_time_ceiling")
                self.assertIsNone(sentinel.poll())
                time.sleep(0.1)
                for pid in json.loads((root / "pids.json").read_text()):
                    stat = Path("/proc") / str(pid) / "stat"
                    self.assertTrue(not stat.exists() or stat.read_text().split(") ")[1].startswith("Z "))
            finally:
                sentinel.terminate()
                sentinel.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
