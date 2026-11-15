"""Fixed worker-side convenience entrypoint, no manager publication authority."""
from pathlib import Path
import sys

# Supports the installed /opt/service layout and the source checkout.
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here if (_here/'research_harness').is_dir() else _here.parents[1]))
from research_harness.worker_pr import main

if __name__ == '__main__':
    raise SystemExit(main())
