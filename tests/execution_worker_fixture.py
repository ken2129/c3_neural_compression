"""Real worker supervisor with only the GitHub identity read replaced offline."""
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_harness import execution_worker

execution_worker.CONTROL_ROOT=os.environ['C3_TEST_CONTROL_ROOT']
execution_worker.__file__=str(Path(os.environ['C3_TEST_RUNTIME'])/'research_harness/execution_worker.py')
class OfflineAPI:
    def __init__(self,token):pass
    def request(self,method,path):
        if (method,path)!=('GET','/user'):raise RuntimeError('offline test forbids network')
        return dict(id=123456789,login='offline-worker-bot')
execution_worker.WorkerGitHub=OfflineAPI
if __name__=='__main__':execution_worker.main()
