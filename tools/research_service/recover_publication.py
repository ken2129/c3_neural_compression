"""Fixed publication-only process. No controller, model or worker entry point."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, '/opt/service')
from research_harness.publication_recovery import recover_publication, read_protected_fence
from research_harness.manager_github import ManagerGitHub
from research_harness.owner_approval import GitHubOwnerComments
from research_harness.result_publisher import GhResultTransport
from research_harness.schema import ContractError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', required=True)
    parser.parse_args()
    if os.getresuid() != (10001,)*3 or os.getresgid() != (10001,)*3 or os.getgroups() not in ([], [10001]):
        raise ContractError('publication-only manager identity required')
    os.umask(0o077)
    # Use only the previously provisioned manager gh configuration. No token,
    # proxy, debug or alternate config inherited; no credential copy or login.
    os.environ.clear()
    os.environ.update(ManagerGitHub._environment())
    fence = read_protected_fence(Path('/control/publication-recovery-77.json'))
    if fence.get('mode') not in ('prepare', 'publish'):
        raise ContractError('explicit protected recovery mode required')
    source = Path('/opt/service/source-sha').read_text().strip()
    if Path('/etc/hostname').read_text().strip() != fence['recovery']['Id'][:12]:
        raise ContractError('recovery deployment identity changed')
    transport = GhResultTransport(enabled=True, manager_credentials_isolated=True, runtime_verified=True)
    result = recover_publication('/control/state', '/control/publication-recovery',
        fence=fence, source_sha=source, transport=transport,
        issue_client=ManagerGitHub(), comments_client=GitHubOwnerComments(),
        publish=fence['mode']=='publish')
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    try: main()
    except Exception:
        print('{"phase":"PUBLICATION_RECOVERY_BLOCKED"}', flush=True)
        raise SystemExit(1) from None
