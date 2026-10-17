"""Safe fixture-only CLI. Deliberately no live run/deploy/merge command."""
import argparse
import json
from pathlib import Path
import tempfile

from research_harness.controller import capabilities, demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('demo', 'capabilities'))
    args = parser.parse_args()
    if args.operation == 'capabilities':
        result = capabilities()
    else:
        with tempfile.TemporaryDirectory(prefix='c3-harness-fixture-') as root:
            result = demo(Path(root) / 'new-run')
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
