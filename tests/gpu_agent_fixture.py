"""Dedicated Linux agent process with synthetic GPU observations and CPU commands."""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_harness.gpu_agent import RecipeAgent, Control

GPU='GPU-'+'a'*32
if __name__=='__main__':
    registry=json.loads(Path(sys.argv[1]).read_text())
    busy=sys.argv[2]=='busy'
    def observe():return ({GPU:(100,4096)},{GPU:1}) if busy else ({GPU:(0,0)}, {})
    control=Control(sys.stdin.buffer.raw,sys.stdout.buffer)
    agent=RecipeAgent(registry,control,observe=observe)
    value=agent.execute(control.receive(10))
    control.emit({'op':'result','result':value})
