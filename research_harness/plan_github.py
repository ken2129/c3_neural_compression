"""Explicit, one-Issue plan publication with existing gh/git authentication.

No Codex launch, credentials setup, polling, or experiment execution. The operator
must review the plan content first. Git updates use an expected-head lease.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .planning import (IssueSnapshot, PlanDraft, PRSnapshot, PublicationPermit,
                       discover, publish_plan, digest)
from .schema import ContractError, REPOSITORY, loads, SHA


class GitHubPlans:
    def __init__(self):
        self.repo = REPOSITORY

    def api(self, path, method='GET', data=None):
        # Fixed caller-constructed repository endpoints, not Issue-controlled URLs.
        if not path.startswith(f'repos/{self.repo}/'):
            raise ContractError('endpoint outside repository')
        command = ['gh', 'api', '--method', method, path]
        if data is not None:
            command += ['--input', '-']
        result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True, timeout=60, check=True)
        return loads(result.stdout) if result.stdout.strip() else None

    def get_issue(self, number):
        # validated before interpolation even for direct adapter callers
        from .schema import positive
        positive(number, integer=True)
        data = self.api(f'repos/{self.repo}/issues/{number}')
        return IssueSnapshot(data['number'], data['title'], data['body'] or '',
                             data['updated_at'], data['state'], self.repo,
                             'pull_request' in data).validate()

    @staticmethod
    def branch(issue):
        from .schema import positive
        positive(issue, integer=True)
        return f'research-plan/issue-{issue}'

    def pr_body(self, issue):
        return (f'## 何をするPRか\n\nIssue #{issue} の実験計画を確認するためのPRです。\n'
                '実験はまだ開始しません。条件・使用量の上限を確認してから、別途実行を承認します。\n\n'
                f'計画の詳細は、このPRの Files changed にある `docs/research-plans/issue-{issue}/plan.md` を開いてください。\n'
                '目的、再利用する結果、新しい作業、比較条件、評価方法、未決定事項をまとめています。\n\n'
                f'Refs #{issue}\n\n計画変更後は以前の承認を引き継ぎません。マージや実験起動はこの提出機能では行いません。\n')

    def find_plan_prs(self, issue):
        branch = self.branch(issue)
        # Explicit cross-references from other PRs require human reconciliation.
        page = 1
        while True:
            events = self.api(f'repos/{self.repo}/issues/{issue}/timeline?per_page=100&page={page}')
            for event in events:
                linked = event.get('source', {}).get('issue', {})
                if event.get('event') == 'cross-referenced' and 'pull_request' in linked:
                    if not linked.get('html_url', '').startswith(f'https://github.com/{self.repo}/pull/'):
                        raise ContractError('cross-repository PR reference requires review')
                    linked_pr = self.api(f'repos/{self.repo}/pulls/{linked["number"]}')
                    if linked_pr['head']['ref'] != branch:
                        raise ContractError('Issue already referenced by another PR; reconcile explicitly')
            if len(events) < 100:
                break
            page += 1
            if page > 20:
                raise ContractError('Issue history exceeds bounded lookup')
        # Include closed/merged PRs so a rerun never silently makes a second PR.
        prs = self.api(f'repos/{self.repo}/pulls?state=all&head=ken2129:{branch}&per_page=100')
        result = []
        for p in prs:
            if p['head']['ref'] != branch or p['head']['repo']['full_name'] != self.repo:
                raise ContractError('unexpected PR owner or branch')
            if p['base']['ref'] != 'main' or p['base']['repo']['full_name'] != self.repo:
                raise ContractError('unexpected PR base')
            head = p['head']['sha']
            files = self.api(f'repos/{self.repo}/contents/docs/research-plans/issue-{issue}/plan.json?ref={head}')
            import base64
            data = loads(base64.b64decode(files['content']).decode())
            if data.get('kind') != 'proposal_only' or data.get('issue') != issue:
                raise ContractError('branch contains another plan')
            changed = self.api(f'repos/{self.repo}/pulls/{p["number"]}/files?per_page=100')
            if p.get('changed_files', len(changed)) > 100 or sorted(f['filename'] for f in changed) != [f'docs/research-plans/issue-{issue}/plan.json', f'docs/research-plans/issue-{issue}/plan.md']:
                raise ContractError('plan PR contains unexpected files')
            if any(f.get('status') == 'renamed' or 'previous_filename' in f for f in changed):
                raise ContractError('renamed files require explicit review')
            parsed = PlanDraft.parse(data)
            md = self.api(f'repos/{self.repo}/contents/docs/research-plans/issue-{issue}/plan.md?ref={head}')
            if base64.b64decode(md['content']).decode() != parsed.markdown():
                raise ContractError('rendered plan readback mismatch')
            result.append(PRSnapshot(p['number'], issue, 'merged' if p['merged_at'] else p['state'],
                                     head, digest(data), True, p.get('body') == self.pr_body(issue)))
        return tuple(result)

    def write_plan(self, *, issue, path, content, body, expected_head, proposal_digest):
        branch = self.branch(issue)
        if path != f'docs/research-plans/issue-{issue}/plan.json':
            raise ContractError('only the exact plan file may be published')
        plan = PlanDraft.parse(loads(content), self.get_issue(issue))
        commit = self.api(f'repos/{self.repo}/commits/{plan.source_sha}')
        if commit.get('sha') != plan.source_sha:
            raise ContractError('source commit not found in repository')
        if plan.fingerprint != proposal_digest or body != plan.markdown():
            raise ContractError('publication content mismatch')
        if expected_head is not None and (type(expected_head) is not str or not SHA.fullmatch(expected_head)):
            raise ContractError('invalid expected head')
        # Repository code is never checked out or executed. Git hooks are disabled.
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        for key in tuple(env):
            if key.startswith('GIT_CONFIG_') and key != 'GIT_CONFIG_NOSYSTEM':
                del env[key]
        with tempfile.TemporaryDirectory(prefix='c3-plan-') as directory:
            def git(*args, input=None, check=True):
                return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', directory, *args],
                                      input=input, text=True, capture_output=True, check=check,
                                      timeout=90, env=env)
            git('init', '--bare', '--quiet')
            git('remote', 'add', 'origin', f'https://github.com/{self.repo}.git')
            git('fetch', '--quiet', '--no-tags', 'origin', 'main')
            base = git('rev-parse', 'FETCH_HEAD').stdout.strip()
            # A branch without PR is an interrupted publication. Recover only if
            # its single plan file exactly equals this proposal; never overwrite.
            remote = git('ls-remote', '--heads', 'origin', f'refs/heads/{branch}').stdout.strip()
            actual = remote.split()[0] if remote else None
            if actual != expected_head:
                if expected_head is not None or actual is None:
                    raise ContractError('branch changed since review')
                git('fetch', '--quiet', 'origin', f'refs/heads/{branch}')
                prior = git('show', f'{actual}:{path}').stdout
                changes = git('diff', '--no-renames', '--name-only', base, actual).stdout.splitlines()
                prior_md = git('show', f"{actual}:{path.replace('.json', '.md')}").stdout
                if prior_md != plan.markdown() or loads(prior) != plan.to_dict() or sorted(changes) != [path, path.replace('.json', '.md')]:
                    raise ContractError('existing untracked branch requires review')
                head = actual
            else:
                parent = base
                if actual:
                    git('fetch', '--quiet', 'origin', f'refs/heads/{branch}')
                    parent = actual
                # read-tree keeps every existing tracked file; update only plan.
                git('read-tree', parent)
                blob = git('hash-object', '-w', '--stdin', input=content).stdout.strip()
                git('update-index', '--add', '--cacheinfo', f'100644,{blob},{path}')
                md_blob = git('hash-object', '-w', '--stdin', input=body).stdout.strip()
                git('update-index', '--add', '--cacheinfo', f"100644,{md_blob},{path.replace('.json', '.md')}")
                tree = git('write-tree').stdout.strip()
                env.update(GIT_AUTHOR_NAME='Research plan publisher', GIT_AUTHOR_EMAIL='research-plan@users.noreply.github.com',
                           GIT_COMMITTER_NAME='Research plan publisher', GIT_COMMITTER_EMAIL='research-plan@users.noreply.github.com')
                head = git('commit-tree', tree, '-p', parent,
                           input=f'Propose research plan for #{issue}\n').stdout.strip()
                # If another publisher changed/created branch, push fails; no retry.
                PlanDraft.parse(plan.to_dict(), self.get_issue(issue))
                git('push', '--quiet', f'--force-with-lease=refs/heads/{branch}:{actual or ""}',
                    'origin', f'{head}:refs/heads/{branch}')
            # GitHub unique open-head/base PR prevents simultaneous duplicate create.
            prs = self.find_plan_prs(issue)
            if len(prs) > 1 or (prs and prs[0].state != 'open'):
                raise ContractError('ambiguous or closed plan PR')
            PlanDraft.parse(plan.to_dict(), self.get_issue(issue))
            if prs:
                if prs[0].head_sha != head or prs[0].plan_digest != proposal_digest or not prs[0].body_matches:
                    raise ContractError('concurrent or modified PR; reconcile without metadata overwrite')
            else:
                self.api(f'repos/{self.repo}/pulls', 'POST', dict(title=f'研究計画: Issue #{issue}',
                         head=branch, base='main', body=self.pr_body(issue), draft=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description='明示したIssueの計画を確認・提出（実験は起動しません）')
    parser.add_argument('operation', choices=('inspect', 'publish'))
    parser.add_argument('--issue', type=int, required=True)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--reviewed-digest')
    parser.add_argument('--expected-head', help='既存PRの確認済みhead SHA。初回は指定しない')
    args = parser.parse_args(argv)
    adapter = GitHubPlans()
    selected, skipped = discover(adapter, (args.issue,))
    if skipped:
        raise ContractError('Issue is closed')
    issue = selected[0]
    if args.operation == 'inspect':
        print(json.dumps(dict(issue=vars(issue), issue_fingerprint=issue.fingerprint), ensure_ascii=False, indent=2))
        return
    if not args.plan or not args.reviewed_digest:
        raise ContractError('--plan and --reviewed-digest required after public-content review')
    plan = PlanDraft.parse(loads(args.plan.read_text()), issue)
    if plan.issue != args.issue:
        raise ContractError('selected Issue mismatch')
    receipt = publish_plan(adapter, plan, PublicationPermit(args.reviewed_digest, args.expected_head))
    print(json.dumps(dict(status='PLAN_SUBMITTED_NOT_EXECUTABLE', pr=receipt.number,
                          head=receipt.head_sha, proposal_digest=receipt.plan_digest)))


if __name__ == '__main__':
    main()
