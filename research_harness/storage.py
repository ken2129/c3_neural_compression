"""Optional Box-like transfers in memory only; no API, filesystem deletion or sync.

Receipts bind every file's path, size and SHA256 to an explicitly approved
manifest and destination. All values here are private control-plane records.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Mapping

from .artifacts import (ContractError, FileDigest, StrictRecord, canonical_bytes,
                        digest, integer, relative_path, require_fields, sha, token)


@dataclass(frozen=True)
class TransferManifest(StrictRecord):
    files: tuple[FileDigest, ...]
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        if type(self.files) is not tuple or not self.files or any(type(f) is not FileDigest for f in self.files):
            raise ContractError("Transfer manifest must list every file")
        names = tuple(f.path for f in self.files)
        if names != tuple(sorted(set(names))):
            raise ContractError("Transfer paths must be unique and sorted")

    @classmethod
    def from_bytes(cls, files: Mapping[str, bytes]):
        if type(files) is not dict or any(type(value) is not bytes for value in files.values()):
            raise ContractError("Invalid mock transfer files")
        return cls(tuple(FileDigest(relative_path(path), len(value), digest(value)) for path, value in sorted(files.items())))

    @classmethod
    def from_dict(cls, value):
        require_fields(value, {field.name for field in fields(cls)})
        if type(value["files"]) is not list:
            raise ContractError("Invalid transfer manifest array")
        return cls(tuple(FileDigest.from_dict(f) for f in value["files"]), value["schema_version"])

    @property
    def manifest_digest(self):
        return digest(canonical_bytes(self.to_dict()))

    @property
    def total_bytes(self):
        return sum(file.size_bytes for file in self.files)


@dataclass(frozen=True)
class TransferApproval(StrictRecord):
    operation: str
    source_id: str
    destination_id: str
    manifest_digest: str
    max_bytes: int
    approved: bool
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        if self.operation not in ("fetch", "archive"):
            raise ContractError("Only copy or fetch is supported")
        token(self.source_id)
        token(self.destination_id)
        if self.source_id == self.destination_id:
            raise ContractError("Source and destination must differ")
        sha(self.manifest_digest)
        integer(self.max_bytes)
        if type(self.approved) is not bool:
            raise ContractError("Invalid transfer approval")


@dataclass(frozen=True)
class TransferReceipt(StrictRecord):
    operation: str
    source_id: str
    destination_id: str
    manifest_digest: str
    verified_files: tuple[FileDigest, ...]
    complete: bool
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        if self.operation not in ("fetch", "archive"):
            raise ContractError("Unknown transfer receipt operation")
        token(self.source_id)
        token(self.destination_id)
        sha(self.manifest_digest)
        if type(self.verified_files) is not tuple or any(type(f) is not FileDigest for f in self.verified_files):
            raise ContractError("Invalid file receipts")
        names = tuple(f.path for f in self.verified_files)
        if names != tuple(sorted(set(names))) or type(self.complete) is not bool:
            raise ContractError("Invalid transfer completion receipt")

    @classmethod
    def from_dict(cls, value):
        require_fields(value, {field.name for field in fields(cls)})
        if type(value["verified_files"]) is not list:
            raise ContractError("Invalid receipt array")
        return cls(**{**value, "verified_files": tuple(FileDigest.from_dict(f) for f in value["verified_files"])})


class FakeStorageEndpoint:
    """Byte dictionary with capacity; deliberately has no delete/sync method."""
    def __init__(self, endpoint_id: str, files: Mapping[str, bytes] | None = None,
                 *, capacity_bytes: int = 32 * 1024 * 1024):
        self.endpoint_id = token(endpoint_id)
        self.capacity_bytes = integer(capacity_bytes)
        self.files = dict(files or {})
        for path, content in self.files.items():
            relative_path(path)
            if type(content) is not bytes:
                raise ContractError("Mock storage only accepts bytes")
        if self.used_bytes > self.capacity_bytes:
            raise ContractError("Initial mock storage exceeds capacity")
        self.write_count = 0

    @property
    def used_bytes(self):
        return sum(len(value) for value in self.files.values())

    @property
    def free_bytes(self):
        return self.capacity_bytes - self.used_bytes

    def copy_new(self, path: str, content: bytes):
        relative_path(path)
        if type(content) is not bytes:
            raise ContractError("Mock storage only accepts bytes")
        if path in self.files:
            raise ContractError("Existing artifacts cannot be overwritten")
        if len(content) > self.free_bytes:
            raise ContractError("Insufficient mock storage capacity")
        self.files[path] = content
        self.write_count += 1


def _matches(endpoint, expected):
    content = endpoint.files.get(expected.path)
    return type(content) is bytes and len(content) == expected.size_bytes and digest(content) == expected.sha256


def transfer(source: FakeStorageEndpoint, destination: FakeStorageEndpoint,
             manifest: TransferManifest, approval: TransferApproval, *,
             previous_receipt: TransferReceipt | None = None,
             interrupt_after: int | None = None) -> TransferReceipt:
    """Resume missing copies only, rehash all present files, and never overwrite.

    ``interrupt_after`` injects a fake connection loss after N new copies. A
    partial receipt is not input-ready or archive-verified. No run is launched.
    """
    if type(source) is not FakeStorageEndpoint or type(destination) is not FakeStorageEndpoint:
        raise ContractError("Live storage unsupported in MVP0")
    if type(manifest) is not TransferManifest or type(approval) is not TransferApproval:
        raise ContractError("Invalid transfer contract")
    if interrupt_after is not None:
        integer(interrupt_after)
    if (approval.approved is not True or approval.source_id != source.endpoint_id
            or approval.destination_id != destination.endpoint_id or approval.manifest_digest != manifest.manifest_digest
            or manifest.total_bytes > approval.max_bytes):
        raise ContractError("Transfer needs matching explicit approval")
    if previous_receipt is not None:
        if type(previous_receipt) is not TransferReceipt or (previous_receipt.operation, previous_receipt.source_id,
                previous_receipt.destination_id, previous_receipt.manifest_digest) != (
                approval.operation, source.endpoint_id, destination.endpoint_id, manifest.manifest_digest):
            raise ContractError("Receipt does not belong to this transfer")
        expected = {file.path: file for file in manifest.files}
        if any(expected.get(file.path) != file or not _matches(destination, file) for file in previous_receipt.verified_files):
            raise ContractError("Previously verified artifact changed or disappeared")
        if previous_receipt.complete and previous_receipt.verified_files != manifest.files:
            raise ContractError("Completion receipt omits required files")
    # Preflight the entire source and every destination conflict before copying.
    for file in manifest.files:
        if not _matches(source, file):
            raise ContractError("Source file hash or size mismatch")
        if file.path in destination.files and not _matches(destination, file):
            raise ContractError("Destination conflict; overwrite forbidden")
    missing_bytes = sum(file.size_bytes for file in manifest.files if file.path not in destination.files)
    if missing_bytes > destination.free_bytes:
        raise ContractError("Insufficient capacity for the complete transfer")
    copied = 0
    verified = []
    for file in manifest.files:
        if file.path not in destination.files:
            if interrupt_after is not None and copied >= interrupt_after:
                break
            destination.copy_new(file.path, source.files[file.path])
            copied += 1
        if not _matches(destination, file):
            raise ContractError("Destination readback hash or size mismatch")
        verified.append(file)
    return TransferReceipt(approval.operation, source.endpoint_id, destination.endpoint_id,
                           manifest.manifest_digest, tuple(verified), len(verified) == len(manifest.files))


@dataclass(frozen=True)
class CleanupApproval(StrictRecord):
    local_id: str
    archive_id: str
    manifest_digest: str
    paths: tuple[str, ...]
    approved: bool
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        token(self.local_id)
        token(self.archive_id)
        if self.local_id == self.archive_id:
            raise ContractError("Cleanup requires a separate archive destination")
        sha(self.manifest_digest)
        if type(self.paths) is not tuple or not self.paths or len(set(self.paths)) != len(self.paths):
            raise ContractError("Cleanup must name exact unique paths")
        for path in self.paths:
            relative_path(path)
        if type(self.approved) is not bool:
            raise ContractError("Invalid cleanup approval")

    @classmethod
    def from_dict(cls, value):
        require_fields(value, {field.name for field in fields(cls)})
        if type(value["paths"]) is not list:
            raise ContractError("Invalid cleanup paths")
        return cls(**{**value, "paths": tuple(value["paths"])})


@dataclass(frozen=True)
class CleanupDecision(StrictRecord):
    eligible: bool
    reason: str
    candidate_bytes: int = 0
    deletion_executed: bool = False
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        if type(self.eligible) is not bool or self.deletion_executed is not False or self.reason not in (
                "CLEANUP_APPROVAL_REQUIRED", "ARCHIVE_NOT_VERIFIED", "CLEANUP_SCOPE_MISMATCH",
                "ACTIVE_OR_PLANNED_DEPENDENCY", "RESTORE_UNCONFIRMED", "CURRENT_HASH_MISMATCH",
                "MOCK_ELIGIBLE_NO_DELETION_IMPLEMENTED"):
            raise ContractError("Invalid cleanup decision")
        integer(self.candidate_bytes)


def cleanup_gate(local: FakeStorageEndpoint, archive: FakeStorageEndpoint,
                 manifest: TransferManifest, receipt: TransferReceipt,
                 approval: CleanupApproval, *, active_dependencies: tuple[str, ...] = (),
                 planned_dependencies: tuple[str, ...] = (), restore_available: bool = False) -> CleanupDecision:
    """Assess only. No deletion is implemented, even after explicit approval."""
    if any(type(obj) is not kind for obj, kind in ((local, FakeStorageEndpoint), (archive, FakeStorageEndpoint),
                                                  (manifest, TransferManifest), (receipt, TransferReceipt), (approval, CleanupApproval))):
        raise ContractError("Invalid cleanup contract")
    if type(active_dependencies) is not tuple or type(planned_dependencies) is not tuple:
        raise ContractError("Dependency observations must be immutable")
    for path in active_dependencies + planned_dependencies:
        relative_path(path)
    if (approval.approved is not True or approval.local_id != local.endpoint_id or approval.archive_id != archive.endpoint_id
            or approval.manifest_digest != manifest.manifest_digest):
        return CleanupDecision(False, "CLEANUP_APPROVAL_REQUIRED")
    if (not receipt.complete or receipt.operation != "archive" or receipt.manifest_digest != manifest.manifest_digest
            or receipt.source_id != local.endpoint_id or receipt.destination_id != archive.endpoint_id
            or receipt.verified_files != manifest.files):
        return CleanupDecision(False, "ARCHIVE_NOT_VERIFIED")
    files = {file.path: file for file in manifest.files}
    if not set(approval.paths) <= set(files):
        return CleanupDecision(False, "CLEANUP_SCOPE_MISMATCH")
    if set(approval.paths) & set(active_dependencies + planned_dependencies):
        return CleanupDecision(False, "ACTIVE_OR_PLANNED_DEPENDENCY")
    if restore_available is not True:
        return CleanupDecision(False, "RESTORE_UNCONFIRMED")
    if any(not _matches(local, file) or not _matches(archive, file) for file in manifest.files):
        return CleanupDecision(False, "CURRENT_HASH_MISMATCH")
    return CleanupDecision(True, "MOCK_ELIGIBLE_NO_DELETION_IMPLEMENTED", sum(files[path].size_bytes for path in approval.paths))
