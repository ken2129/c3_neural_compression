"""Installed stdio agent, invoked by the manager in an existing work container."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research_harness.gpu_agent import main

if __name__ == '__main__':
    try:
        main()
    except BaseException:
        # Agent/SSH stderr is private. Never export exception text or paths.
        print('GPU_EXECUTOR_REJECTED', file=sys.stderr, flush=True)
        raise SystemExit(1)
