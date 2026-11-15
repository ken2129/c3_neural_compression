"""Byte provenance for the three approved public inputs (standard library only)."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

SOURCE_SHA = "a3c285fb191f95bdf3ecea54408c53adb177b5c1"
INPUTS = (
    "docs/research/assets/issue50_four_method.json",
    "docs/research/assets/issue50_four_method.csv",
    "docs/research/2026-10-16_four_method_comparison.md",
)


def collect(root):
    files = []
    for name in INPUTS:
        data = (Path(root) / name).read_bytes()
        files.append({"path": name, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})
    return {"source_sha": SOURCE_SHA, "files": files}


def encode(manifest):
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--check", type=Path, help="Compare current bytes with an existing manifest")
    args = parser.parse_args(argv)
    try:
        actual = collect(args.root)
        if args.check:
            expected = json.loads(args.check.read_text(encoding="utf-8"))
            if actual != expected:
                print("provenance mismatch", file=sys.stderr)
                return 1
            print("OK: 3 inputs match manifest")
        else:
            sys.stdout.write(encode(actual))
        return 0
    except (OSError, ValueError) as exc:
        print(f"provenance error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
