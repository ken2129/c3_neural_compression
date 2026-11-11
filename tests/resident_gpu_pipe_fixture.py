"""Modeled CPU broker over real pipes; no UID/cgroup/model/publication claims."""
from pathlib import Path
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_harness.schema import canonical, loads
from research_harness.gpu_contract import validate_gpu_result


def send(value):sys.stdout.buffer.write(canonical(value)+b'\n');sys.stdout.buffer.flush()


if __name__=='__main__':
    root=Path(sys.argv[1]);plan=None;gpu=None;events=[]
    for line in sys.stdin.buffer:
        value=loads(line)
        if value['op']=='prepare_goal':
            plan=value['plan'];events.append('prepared');send({'prepared':True})
        elif value['op']=='preflight':
            events.append('preflight');send({'ready':True})
        elif value['op']=='gpu_result':
            gpu=validate_gpu_result(value['result'],binding=value['binding'],request=plan['gpu_experiment'])
            (root/'gpu-result.json').write_bytes(canonical(gpu))
            events.append('gpu_staged');send({'gpu_result_staged':True})
        elif value['op']=='goal':
            if 'gpu_experiment' in plan and gpu is None:raise RuntimeError('GPU stage missing')
            current=value['binding'];send({'op':'recheck'})
            reply=loads(sys.stdin.buffer.readline())
            if reply!={'approved':True}:raise RuntimeError('approval revoked')
            events.append('cpu_analysis')
            send({'direct_pr_result':dict(binding=current,model=plan['model'],
                outcome='completed' if gpu['outcome']=='completed' else 'incomplete',
                summary='Analyzed bounded GPU fixture output; publication is not verified.',
                pr_url='https://github.com/ken2129/c3_neural_compression/pull/123',elapsed_seconds=.1)})
        elif value['op']=='job_finished':
            events.append('cleaned');(root/'events.json').write_bytes(canonical(events))
            send({'cleaned':True});break
        else:raise RuntimeError('unexpected fixture frame')
