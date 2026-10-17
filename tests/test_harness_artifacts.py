"""CPU-only artifact contracts; fixture success is never real AP validation."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_harness.artifacts import (
    ArtifactSnapshot, ContractError, EvidenceBinding, MockArtifactValidator,
    PublicResult, ValidationEvidence, canonical_bytes, existing_detector_input_rate,
    freeze_artifacts, render_public_result,
)


def fixture_binding(**changes):
    defaults = dict(repository="ken2129/c3_neural_compression", issue_number=900001,
                    request_id="request-1", run_id="run-1", plan_revision=1,
                    plan_digest="a" * 64, head_sha="b" * 40, base_sha="c" * 40,
                    policy_version="mock-policy-v1")
    return EvidenceBinding(**{**defaults, **changes})


def fixture_bundle():
    return {"schema_version": 1, "fixture_only": True, "source_commit": "b" * 40,
            "input_protocol": "detector_input_v1", "outcome": "negative", "cpu_seconds": 2,
            "reported_mean_bpp": 1.5,
            "rows": [dict(image_key="sample-1", status="complete", height=10, width=10,
                          latent_bits=80, synthesis_bits=10, entropy_bits=10, total_bits=100),
                     dict(image_key="sample-2", status="complete", height=20, width=10,
                          latent_bits=320, synthesis_bits=40, entropy_bits=40, total_bits=400)]}


def fixture_evidence(bundle=None, binding=None):
    snapshot = ArtifactSnapshot.from_bytes({"mock_measurements.json": canonical_bytes(bundle or fixture_bundle())})
    evidence = MockArtifactValidator().validate(snapshot, binding or fixture_binding(), ("sample-1", "sample-2"))
    return snapshot, evidence


class ArtifactTests(unittest.TestCase):
    def test_negative_result_uses_mean_not_pooled_rate(self):
        snapshot, evidence = fixture_evidence()
        self.assertEqual(evidence.public_result.outcome, "negative")
        self.assertEqual(evidence.public_result.mean_estimated_bpp, 1.5)
        self.assertNotEqual(evidence.public_result.mean_estimated_bpp, 500 / 300)
        self.assertEqual(evidence.artifact_digest, snapshot.manifest_digest)
        self.assertEqual(ValidationEvidence.from_dict(evidence.to_dict()), evidence)
        self.assertTrue(render_public_result(evidence)["fixture_only"])
        self.assertIn("no_coco_ap", render_public_result(evidence)["validation_scope"])

    def test_missing_reordered_failed_and_extra_rows_rejected(self):
        for change in (lambda b: b["rows"].pop(), lambda b: b["rows"].reverse(),
                       lambda b: b["rows"][0].update(status="failed"),
                       lambda b: b["rows"].append(copy.deepcopy(b["rows"][0]))):
            bundle = fixture_bundle()
            change(bundle)
            with self.assertRaises(ContractError):
                fixture_evidence(bundle)

    def test_wrong_source_protocol_total_and_stale_rate_rejected(self):
        for change in (lambda b: b.update(source_commit="d" * 40),
                       lambda b: b.update(input_protocol="original_rgb"),
                       lambda b: b.update(reported_mean_bpp=500 / 300),
                       lambda b: b["rows"][0].update(total_bits=90),
                       lambda b: b["rows"][0].update(width=0),
                       lambda b: b.update(fixture_only=False),
                       lambda b: b.update(schema_version=True)):
            bundle = fixture_bundle()
            change(bundle)
            with self.assertRaises(ContractError):
                fixture_evidence(bundle)

    def test_unknown_fields_and_private_text_rejected_without_echo(self):
        for key, value in (("private_path", "/private/secret-sentinel"),
                           ("image_ids", [123]), ("model_hash", "d" * 64),
                           ("official_cocoeval_executed", True)):
            bundle = fixture_bundle()
            bundle[key] = value
            with self.assertRaises(ContractError) as caught:
                fixture_evidence(bundle)
            self.assertNotIn("sentinel", str(caught.exception))
        public = render_public_result(fixture_evidence()[1])
        for key in ("outcome", "validation_scope", "rate_definition", "head_sha"):
            bad = {**public, key: "/private/secret-sentinel"}
            with self.assertRaises(ContractError) as caught:
                render_public_result(bad)
            self.assertNotIn("sentinel", str(caught.exception))
        with self.assertRaises(ContractError):
            render_public_result({**public, "private_path": "/private/secret-sentinel"})

    def test_nan_infinity_booleans_and_extreme_integer_rejected(self):
        public = render_public_result(fixture_evidence()[1])
        for value in (float("nan"), float("inf"), -float("inf"), True, 10**400):
            with self.assertRaises(ContractError):
                render_public_result({**public, "mean_estimated_bpp": value})
        for invalid in (b'{"schema_version":1,"schema_version":1}', b'{"value":NaN}'):
            with self.assertRaises(ContractError):
                MockArtifactValidator().validate(ArtifactSnapshot.from_bytes({"mock_measurements.json": invalid}), fixture_binding(), ("sample-1",))

    def test_evidence_binds_source_versions_and_immutable_bytes(self):
        snapshot, evidence = fixture_evidence()
        self.assertNotEqual(snapshot.manifest_digest, ArtifactSnapshot.from_bytes({"mock_measurements.json": b"changed"}).manifest_digest)
        with self.assertRaises(ContractError):
            replace(evidence, binding=replace(evidence.binding, head_sha="d" * 40))
        with self.assertRaises(ContractError):
            replace(evidence, binding=replace(evidence.binding, validator_version="worker-validator"))
        with self.assertRaises(ContractError):
            ValidationEvidence.from_dict({**evidence.to_dict(), "approval": True})

    def test_filesystem_snapshot_survives_change_rejects_symlinks_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_bytes(b"original")
            snapshot = freeze_artifacts(root, ("result.json",))
            (root / "result.json").write_bytes(b"modified")
            self.assertEqual(snapshot.read("result.json"), b"original")
            (root / "link").symlink_to(root / "result.json")
            (root / "nested").symlink_to(root, target_is_directory=True)
            for path in ("link", "nested/result.json", "../result.json", "/etc/passwd", "a/../result.json"):
                with self.assertRaises(ContractError):
                    freeze_artifacts(root, (path,))
            with self.assertRaises(ContractError):
                freeze_artifacts(root, ("result.json",), max_total_bytes=2)
            with self.assertRaises(ContractError):
                freeze_artifacts(root, ("missing",))

    def test_lazy_adapter_delegates_to_existing_protocol(self):
        class Protocol:
            DETECTOR_INPUT = "detector_input_v1"
            @staticmethod
            def rate_summary(*args):
                return args
        with patch("research_harness.artifacts.importlib.import_module", return_value=Protocol) as load:
            result = existing_detector_input_rate({"manifest": True}, [{"bits": 1}], "c3_image")
        load.assert_called_once_with("c3_neural_compression.evaluation.detector_input_protocol")
        self.assertEqual(result[-1], "detector_input_v1")
        self.assertEqual(result[-2], "c3_image")

    def test_real_existing_rate_helper_runs_without_science_libraries(self):
        # Repository package name is normally supplied by its parent checkout.
        # A subprocess-only namespace alias keeps this test installation-free.
        script = r"""
import pathlib, sys, types
package = types.ModuleType('c3_neural_compression')
package.__path__ = [str(pathlib.Path.cwd())]
sys.modules['c3_neural_compression'] = package
from research_harness.artifacts import existing_detector_input_rate
from c3_neural_compression.evaluation.shared_detector import expected_resized_shape
manifest = {'schema_version': 1, 'image_ids': [1, 2], 'images': [
    {'image_id': 1, 'original_hw': [100, 100], 'path': 'synthetic-one'},
    {'image_id': 2, 'original_hw': [100, 1000], 'path': 'synthetic-two'}]}
rows = []
for image, bits in zip(manifest['images'], [100, 200]):
    hw = expected_resized_shape(*image['original_hw'])
    rows.append(dict(image_id=image['image_id'], status='ok', rate_definition='entropy_estimated',
        original_hw=image['original_hw'], detector_hw=hw, codec_hw=hw,
        codec_input_protocol='detector_input_v1', estimated_bits={
        'latents': bits * .8, 'synthesis_network': bits * .1, 'entropy_network': bits * .1, 'total': bits}))
result = existing_detector_input_rate(manifest, rows, 'c3_image')
expected = sum(row['estimated_bits']['total'] / (row['detector_hw'][0] * row['detector_hw'][1]) for row in rows) / 2
assert result['estimated_bpp'] == expected
assert result['input_protocol'] == 'detector_input_v1'
assert not any(n in sys.modules for n in ('torch', 'jax', 'numpy', 'detectron2'))
rows[0]['codec_hw'] = [832, 832]
try:
    existing_detector_input_rate(manifest, rows, 'c3_image')
except ValueError:
    pass
else:
    raise AssertionError('Padded dimensions were accepted')
"""
        subprocess.run([sys.executable, "-c", script], check=True)

    def test_fresh_core_import_does_not_load_science_dependencies(self):
        script = "import sys; import research_harness.artifacts, research_harness.publication, research_harness.storage; assert not any(n in sys.modules for n in ('torch','jax','numpy','detectron2'))"
        subprocess.run([sys.executable, "-c", script], check=True)


if __name__ == "__main__":
    unittest.main()
