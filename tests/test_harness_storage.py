"""Optional storage is mock-only, resumable, immutable, and never deletes."""
from dataclasses import replace
import unittest

from research_harness.artifacts import ContractError
from research_harness.storage import (CleanupApproval, FakeStorageEndpoint, TransferApproval,
    TransferManifest, TransferReceipt, cleanup_gate, transfer)


def storage_fixture(operation="archive"):
    files = {"bits.json": b"bits", "predictions.json": b"predictions", "report.json": b"report"}
    source = FakeStorageEndpoint("local-run-1", files)
    destination = FakeStorageEndpoint("mock-box-run-1")
    manifest = TransferManifest.from_bytes(files)
    approval = TransferApproval(operation, source.endpoint_id, destination.endpoint_id,
                                manifest.manifest_digest, manifest.total_bytes, True)
    return source, destination, manifest, approval


class StorageTests(unittest.TestCase):
    def test_full_receipt_checks_all_sizes_and_hashes(self):
        source, destination, manifest, approval = storage_fixture()
        receipt = transfer(source, destination, manifest, approval)
        self.assertTrue(receipt.complete)
        self.assertEqual(receipt.verified_files, manifest.files)
        self.assertEqual(source.files, destination.files)
        self.assertEqual(destination.write_count, 3)
        self.assertEqual(transfer(source, destination, manifest, approval, previous_receipt=receipt), receipt)
        self.assertEqual(destination.write_count, 3)

    def test_partial_resume_copies_only_missing_files(self):
        source, destination, manifest, approval = storage_fixture()
        partial = transfer(source, destination, manifest, approval, interrupt_after=1)
        self.assertFalse(partial.complete)
        self.assertEqual(len(partial.verified_files), 1)
        self.assertEqual(destination.write_count, 1)
        restored = TransferReceipt.from_dict(partial.to_dict())
        complete = transfer(source, destination, manifest, approval, previous_receipt=restored)
        self.assertTrue(complete.complete)
        self.assertEqual(destination.write_count, 3)
        self.assertEqual(source.files, destination.files)

    def test_new_destination_new_manifest_over_limit_and_unapproved_block(self):
        source, destination, manifest, approval = storage_fixture()
        for change in ({"approved": False}, {"destination_id": "different-box"},
                       {"source_id": "other-source"}, {"manifest_digest": "d" * 64},
                       {"max_bytes": manifest.total_bytes - 1}):
            with self.assertRaises(ContractError):
                transfer(source, destination, manifest, replace(approval, **change))
            self.assertEqual(destination.write_count, 0)

    def test_source_corruption_missing_and_destination_conflict_preflight(self):
        for change in ("missing", "same_size_corruption", "wrong_size"):
            source, destination, manifest, approval = storage_fixture()
            if change == "missing":
                source.files.pop("report.json")
            else:
                source.files["report.json"] = b"xxxxxx" if change == "same_size_corruption" else b"x"
            with self.assertRaises(ContractError):
                transfer(source, destination, manifest, approval)
            self.assertEqual(destination.write_count, 0)
        source, destination, manifest, approval = storage_fixture()
        destination.files["report.json"] = b"other"
        with self.assertRaises(ContractError):
            transfer(source, destination, manifest, approval)
        self.assertEqual(destination.files, {"report.json": b"other"})
        self.assertEqual(destination.write_count, 0)

    def test_capacity_and_path_traversal_block(self):
        source, destination, manifest, approval = storage_fixture()
        small = FakeStorageEndpoint(destination.endpoint_id, capacity_bytes=manifest.total_bytes - 1)
        with self.assertRaises(ContractError):
            transfer(source, small, manifest, approval)
        self.assertEqual(small.write_count, 0)
        for path in ("../secret", "/private/file", "a/../file", "a\\file"):
            with self.assertRaises(ContractError):
                TransferManifest.from_bytes({path: b"data"})

    def test_changed_receipt_destination_must_not_be_overwritten_on_resume(self):
        source, destination, manifest, approval = storage_fixture()
        partial = transfer(source, destination, manifest, approval, interrupt_after=1)
        destination.files[partial.verified_files[0].path] = b"evil"
        with self.assertRaises(ContractError):
            transfer(source, destination, manifest, approval, previous_receipt=partial)
        self.assertEqual(destination.write_count, 1)
        self.assertEqual(destination.files[partial.verified_files[0].path], b"evil")

    def test_forged_complete_or_cross_transfer_receipt_rejected(self):
        source, destination, manifest, approval = storage_fixture()
        partial = transfer(source, destination, manifest, approval, interrupt_after=1)
        for receipt in (replace(partial, complete=True), replace(partial, destination_id="different-box"),
                        replace(partial, manifest_digest="f" * 64)):
            with self.assertRaises(ContractError):
                transfer(source, destination, manifest, approval, previous_receipt=receipt)
        self.assertEqual(destination.write_count, 1)

    def test_fetch_verification_does_not_start_experiment(self):
        source, destination, manifest, approval = storage_fixture("fetch")
        partial = transfer(source, destination, manifest, approval, interrupt_after=0)
        self.assertFalse(partial.complete)
        self.assertEqual(destination.files, {})
        full = transfer(source, destination, manifest, approval, previous_receipt=partial)
        self.assertEqual(full.operation, "fetch")
        self.assertTrue(full.complete)

    def test_cleanup_eligibility_never_executes_deletion(self):
        source, destination, manifest, approval = storage_fixture()
        receipt = transfer(source, destination, manifest, approval)
        cleanup = CleanupApproval(source.endpoint_id, destination.endpoint_id,
                                  manifest.manifest_digest, ("bits.json",), True)
        local_before, archive_before = dict(source.files), dict(destination.files)
        decision = cleanup_gate(source, destination, manifest, receipt, cleanup, restore_available=True)
        self.assertTrue(decision.eligible)
        self.assertFalse(decision.deletion_executed)
        self.assertEqual(decision.candidate_bytes, 4)
        self.assertEqual(source.files, local_before)
        self.assertEqual(destination.files, archive_before)
        self.assertFalse(hasattr(destination, "delete"))
        self.assertFalse(hasattr(destination, "sync"))

    def test_cleanup_requires_approval_complete_archive_fresh_hash_and_dependencies(self):
        source, destination, manifest, approval = storage_fixture()
        receipt = transfer(source, destination, manifest, approval)
        cleanup = CleanupApproval(source.endpoint_id, destination.endpoint_id,
                                  manifest.manifest_digest, ("bits.json",), True)
        for kwargs in ({}, {"restore_available": True, "active_dependencies": ("bits.json",)},
                       {"restore_available": True, "planned_dependencies": ("bits.json",)}):
            self.assertFalse(cleanup_gate(source, destination, manifest, receipt, cleanup, **kwargs).eligible)
        for altered in (replace(cleanup, approved=False), replace(cleanup, paths=("unknown",)),
                        replace(cleanup, archive_id="other-box")):
            self.assertFalse(cleanup_gate(source, destination, manifest, receipt, altered, restore_available=True).eligible)
        for bad in (replace(receipt, complete=False), replace(receipt, operation="fetch"),
                    replace(receipt, verified_files=receipt.verified_files[:1])):
            self.assertFalse(cleanup_gate(source, destination, manifest, bad, cleanup, restore_available=True).eligible)
        source.files["bits.json"] = b"evil"
        self.assertFalse(cleanup_gate(source, destination, manifest, receipt, cleanup, restore_available=True).eligible)
        source.files["bits.json"] = b"bits"
        destination.files["bits.json"] = b"evil"
        self.assertFalse(cleanup_gate(source, destination, manifest, receipt, cleanup, restore_available=True).eligible)

    def test_schema_roundtrips_unknown_fields_and_live_transport_rejected(self):
        source, destination, manifest, approval = storage_fixture()
        receipt = transfer(source, destination, manifest, approval)
        cleanup = CleanupApproval(source.endpoint_id, destination.endpoint_id,
                                  manifest.manifest_digest, ("bits.json",), True)
        for record in (manifest, approval, receipt, cleanup):
            self.assertEqual(type(record).from_dict(record.to_dict()), record)
            with self.assertRaises(ContractError):
                type(record).from_dict({**record.to_dict(), "overwrite": True})
        with self.assertRaises(ContractError):
            transfer(object(), destination, manifest, approval)
        with self.assertRaises(ContractError):
            replace(approval, operation="delete")


if __name__ == "__main__":
    unittest.main()
