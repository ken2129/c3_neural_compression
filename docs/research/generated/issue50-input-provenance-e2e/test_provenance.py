"""Synthetic byte fixtures only; approved inputs are never modified."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

import provenance as p


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in p.INPUTS:
            file = self.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"abc")
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(p.encode(p.collect(self.root)), encoding="utf-8")

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = p.main(["--root", str(self.root), *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_known_bytes_and_sha(self):
        result = p.collect(self.root)
        self.assertEqual(result["source_sha"], p.SOURCE_SHA)
        self.assertEqual([f["path"] for f in result["files"]], list(p.INPUTS))
        for file in result["files"]:
            self.assertEqual(file["bytes"], 3)
            self.assertEqual(file["sha256"], "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_deterministic_cli(self):
        self.assertEqual(self.invoke(), (0, self.manifest.read_text(encoding="utf-8"), ""))
        self.assertEqual(self.invoke(), self.invoke())

    def test_matching_manifest(self):
        self.assertEqual(self.invoke("--check", self.manifest), (0, "OK: 3 inputs match manifest\n", ""))

    def test_missing_fixture(self):
        (self.root / p.INPUTS[1]).unlink()
        code, out, err = self.invoke("--check", self.manifest)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("provenance error:", err)

    def test_same_length_modified_fixture(self):
        (self.root / p.INPUTS[0]).write_bytes(b"abd")
        self.assertEqual(self.invoke("--check", self.manifest), (1, "", "provenance mismatch\n"))

    def test_source_sha_mismatch(self):
        self.manifest.write_text(self.manifest.read_text().replace(p.SOURCE_SHA, "0" * 40))
        self.assertEqual(self.invoke("--check", self.manifest)[0], 1)

    def test_input_bytes_unchanged(self):
        before = [(self.root / name).read_bytes() for name in p.INPUTS]
        self.invoke()
        self.invoke("--check", self.manifest)
        self.assertEqual(before, [(self.root / name).read_bytes() for name in p.INPUTS])


if __name__ == "__main__":
    unittest.main()
