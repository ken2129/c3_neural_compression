"""Installed entrypoint inside the experiment container; no manager secrets."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from research_harness.execution_worker import main
if __name__=='__main__':
    try:main()
    except BaseException:
        print('EXECUTION_WORKER_STOPPED',file=sys.stderr)
        raise SystemExit(1)
