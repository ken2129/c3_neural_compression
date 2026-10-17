"""Immutable CPU-fixture evidence and a closed public result schema.

This module validates synthetic rate fixtures only. It does not execute a COCO
AP evaluator or establish OS/credential isolation. Evidence is deliberately
marked fixture-only and must never authorize a live publisher.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import importlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping

SCHEMA_VERSION = 1
VALIDATOR_VERSION = "mock-rate-v1"
RATE_DEFINITION = "mean(total_estimated_bits / detector_resized_nonpadding_pixels)"
OUTCOMES = frozenset(("positive", "negative", "mixed", "inconclusive"))


class ContractError(ValueError):
    """Invalid contract. Messages intentionally omit untrusted values."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ContractError("Value is not canonical finite JSON") from None


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_fields(value: Any, names: set[str]) -> dict:
    if type(value) is not dict or set(value) != names:
        raise ContractError("Missing or unknown schema fields")
    return value


def token(value: Any) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ContractError("Invalid opaque identifier")
    return value


def sha(value: Any, length: int = 64) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
        raise ContractError("Invalid digest")
    return value


def integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError("Invalid integer")
    return value


def number(value: Any, minimum: float = 0) -> float:
    if type(value) not in (int, float):
        raise ContractError("Invalid finite number")
    try:
        valid = math.isfinite(value) and value >= minimum
    except OverflowError:
        valid = False
    if not valid:
        raise ContractError("Invalid finite number")
    return float(value)


def relative_path(value: Any) -> str:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise ContractError("Invalid relative artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ContractError("Invalid relative artifact path")
    if any(ord(char) < 32 for char in value):
        raise ContractError("Invalid relative artifact path")
    return value


class StrictRecord:
    def to_dict(self) -> dict:
        return json.loads(canonical_bytes(asdict(self)))

    @classmethod
    def from_dict(cls, value: dict):
        require_fields(value, {field.name for field in fields(cls)})
        return cls(**value)

    def _version(self):
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ContractError("Unsupported schema version")


@dataclass(frozen=True)
class FileDigest(StrictRecord):
    path: str
    size_bytes: int
    sha256: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        self._version()
        relative_path(self.path)
        integer(self.size_bytes)
        sha(self.sha256)


@dataclass(frozen=True)
class FrozenArtifact:
    descriptor: FileDigest
    content: bytes

    def __post_init__(self):
        if type(self.descriptor) is not FileDigest or type(self.content) is not bytes:
            raise ContractError("Invalid frozen artifact")
        if len(self.content) != self.descriptor.size_bytes or digest(self.content) != self.descriptor.sha256:
            raise ContractError("Artifact hash or size mismatch")


@dataclass(frozen=True)
class ArtifactSnapshot:
    files: tuple[FrozenArtifact, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ContractError("Unsupported snapshot version")
        if type(self.files) is not tuple or not self.files or any(type(f) is not FrozenArtifact for f in self.files):
            raise ContractError("Snapshot must contain immutable artifacts")
        names = tuple(f.descriptor.path for f in self.files)
        if names != tuple(sorted(set(names))):
            raise ContractError("Snapshot paths must be unique and sorted")

    @classmethod
    def from_bytes(cls, files: Mapping[str, bytes]):
        if type(files) is not dict or not files or any(type(content) is not bytes for content in files.values()):
            raise ContractError("Invalid or empty artifact snapshot")
        return cls(tuple(FrozenArtifact(FileDigest(relative_path(path), len(content), digest(content)), content)
                         for path, content in sorted(files.items())))

    @property
    def manifest_digest(self) -> str:
        return digest(canonical_bytes({"schema_version": self.schema_version,
                                       "files": [f.descriptor.to_dict() for f in self.files]}))

    def read(self, path: str) -> bytes:
        relative_path(path)
        for artifact in self.files:
            if artifact.descriptor.path == path:
                return artifact.content
        raise ContractError("Required artifact missing")


def _read_regular(root: Path, relative: str, max_bytes: int) -> bytes:
    """Descriptor-relative, no-follow reads; bytes detach validation from writers."""
    relative_path(relative)
    if not hasattr(os, "O_NOFOLLOW"):
        raise ContractError("No-follow artifact reads unsupported")
    descriptors = []
    try:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(current)
        # Opening every ancestor avoids following a symlink in the trusted root.
        for part in root.absolute().parts[1:] + PurePosixPath(relative).parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        fd = os.open(PurePosixPath(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ContractError("Artifact is not a bounded regular file")
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(result) > max_bytes or identity(before) != identity(after) or len(result) != before.st_size:
            raise ContractError("Artifact changed while snapshotting")
        return result
    except OSError:
        raise ContractError("Artifact cannot be safely snapshotted") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def freeze_artifacts(root: Path | str, relative_paths, *, max_total_bytes: int = 16 * 1024 * 1024) -> ArtifactSnapshot:
    integer(max_total_bytes, 1)
    paths = tuple(relative_paths)
    if not paths or len(paths) != len(set(paths)):
        raise ContractError("Need unique required artifact paths")
    data = {}
    remaining = max_total_bytes
    for path in sorted(paths):
        data[path] = _read_regular(Path(root), path, remaining)
        remaining -= len(data[path])
    return ArtifactSnapshot.from_bytes(data)


@dataclass(frozen=True)
class EvidenceBinding(StrictRecord):
    repository: str
    issue_number: int
    request_id: str
    run_id: str
    plan_revision: int
    plan_digest: str
    head_sha: str
    base_sha: str
    policy_version: str
    validator_version: str = VALIDATOR_VERSION
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        self._version()
        if self.repository != "ken2129/c3_neural_compression":
            raise ContractError("Repository outside fixture policy")
        integer(self.issue_number, 1)
        integer(self.plan_revision, 1)
        for value in (self.request_id, self.run_id, self.policy_version, self.validator_version):
            token(value)
        sha(self.plan_digest)
        sha(self.head_sha, 40)
        sha(self.base_sha, 40)


@dataclass(frozen=True)
class PublicResult(StrictRecord):
    outcome: str
    image_count: int
    mean_estimated_bpp: float
    cpu_seconds: float
    head_sha: str
    base_sha: str
    fixture_only: bool = True
    validation_scope: str = "synthetic_rate_only_no_coco_ap"
    rate_definition: str = RATE_DEFINITION
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        self._version()
        if self.fixture_only is not True or self.validation_scope != "synthetic_rate_only_no_coco_ap":
            raise ContractError("Real scientific publication unsupported")
        if type(self.outcome) is not str or self.outcome not in OUTCOMES or self.rate_definition != RATE_DEFINITION:
            raise ContractError("Public string is not allowlisted")
        integer(self.image_count, 1)
        number(self.mean_estimated_bpp)
        number(self.cpu_seconds)
        sha(self.head_sha, 40)
        sha(self.base_sha, 40)


@dataclass(frozen=True)
class ValidationEvidence(StrictRecord):
    binding: EvidenceBinding
    artifact_digest: str
    public_result: PublicResult
    fixture_only: bool = True
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        self._version()
        if type(self.binding) is not EvidenceBinding or type(self.public_result) is not PublicResult or self.fixture_only is not True:
            raise ContractError("Invalid fixture evidence")
        sha(self.artifact_digest)
        if self.binding.head_sha != self.public_result.head_sha or self.binding.base_sha != self.public_result.base_sha:
            raise ContractError("Public result source mismatch")
        if self.binding.validator_version != VALIDATOR_VERSION:
            raise ContractError("Unsupported validator version")

    @classmethod
    def from_dict(cls, value):
        require_fields(value, {field.name for field in fields(cls)})
        return cls(**{**value, "binding": EvidenceBinding.from_dict(value["binding"]),
                      "public_result": PublicResult.from_dict(value["public_result"])})


def _strict_json(raw: bytes):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ContractError("Duplicate JSON field")
            result[key] = value
        return result
    def invalid(_):
        raise ContractError("Nonfinite JSON number")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError):
        raise ContractError("Invalid artifact JSON") from None


class MockArtifactValidator:
    """Recompute synthetic per-image rate; never certify AP from a worker flag."""

    def validate(self, snapshot: ArtifactSnapshot, binding: EvidenceBinding,
                 expected_image_keys: tuple[str, ...], *, artifact_path: str = "mock_measurements.json") -> ValidationEvidence:
        if type(snapshot) is not ArtifactSnapshot or type(binding) is not EvidenceBinding:
            raise ContractError("Invalid validation inputs")
        if type(expected_image_keys) is not tuple or not expected_image_keys or len(set(expected_image_keys)) != len(expected_image_keys):
            raise ContractError("Expected input set is empty or duplicated")
        for key in expected_image_keys:
            token(key)
        raw = require_fields(_strict_json(snapshot.read(artifact_path)), {
            "schema_version", "fixture_only", "source_commit", "input_protocol", "rows",
            "outcome", "cpu_seconds", "reported_mean_bpp"})
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION or raw["fixture_only"] is not True:
            raise ContractError("Only versioned CPU fixtures are supported")
        if raw["source_commit"] != binding.head_sha or raw["input_protocol"] != "detector_input_v1":
            raise ContractError("Artifact source or input protocol mismatch")
        if type(raw["rows"]) is not list or len(raw["rows"]) != len(expected_image_keys):
            raise ContractError("Required measurement rows missing")
        rates = []
        for expected, row in zip(expected_image_keys, raw["rows"]):
            require_fields(row, {"image_key", "status", "height", "width", "latent_bits", "synthesis_bits", "entropy_bits", "total_bits"})
            if row["image_key"] != expected or row["status"] != "complete":
                raise ContractError("Incomplete or reordered input measurements")
            pixels = integer(row["height"], 1) * integer(row["width"], 1)
            bits = sum(number(row[name]) for name in ("latent_bits", "synthesis_bits", "entropy_bits"))
            if not math.isclose(bits, number(row["total_bits"]), rel_tol=1e-12, abs_tol=1e-12):
                raise ContractError("Estimated bit components disagree")
            rates.append(bits / pixels)
        mean = sum(rates) / len(rates)
        if not math.isclose(mean, number(raw["reported_mean_bpp"]), rel_tol=1e-12, abs_tol=1e-12):
            raise ContractError("Stale or wrong rate aggregate")
        public = PublicResult(raw["outcome"], len(rates), mean, number(raw["cpu_seconds"]), binding.head_sha, binding.base_sha)
        return ValidationEvidence(binding, snapshot.manifest_digest, public)


def render_public_result(evidence: ValidationEvidence | Mapping[str, Any]) -> dict:
    """Closed fields and fixed strings; private metadata/free text are rejected."""
    if type(evidence) is ValidationEvidence:
        return PublicResult.from_dict(evidence.public_result.to_dict()).to_dict()
    return PublicResult.from_dict(evidence).to_dict()


def existing_detector_input_rate(manifest, rows, condition):
    """Lazy reuse of scientific code; rate only, not independent AP validation.

    Requires the existing science environment. No torch/JAX import occurs while
    importing the harness or running CPU fixtures.
    """
    protocol = importlib.import_module("c3_neural_compression.evaluation.detector_input_protocol")
    return protocol.rate_summary(manifest, rows, condition, protocol.DETECTOR_INPUT)
