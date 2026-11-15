#!/usr/bin/python3 -IB
"""Immutable /opt/c3/bin/submit-issue-event, not Actions-workspace code."""
import sys
sys.path.insert(0, '/opt/service')
from research_harness.actions_inbox import submit_main

if __name__ == '__main__':
    raise SystemExit(submit_main())
