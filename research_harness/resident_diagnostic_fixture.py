"""Fixed synthetic data only. No network client or real authentication here."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
from .goal_plan import DIRECT_PR_KIND,GOAL_RESOURCE_BUDGET
from .intake_flow_fixture import FixtureSource
from .issue_intake import API,MARKER,content_hash
from .schema import canonical,REPOSITORY,ContractError
from .worker_pr import default_worker_pr_config,CONSENTS

A,B=920001,920002
WORKFLOW='d'*40
INPUT_SHA='a3c285fb191f95bdf3ecea54408c53adb177b5c1'
DUMMY_TOKEN=b'DIAGNOSTIC-NOT-A-TOKEN'
ACTOR={'login':'resident-diag-worker','id':987654321}


def plan(deployment,number):
    return dict(kind=DIRECT_PR_KIND,deployment_id=deployment,source_sha=INPUT_SHA,
        publish_result_pr=True,model='gpt-6.1-sol',sandbox_mode='externalSandbox',external_network='enabled',
        max_seconds=45,model_usage_approved=True,goal='OFFLINE FIXTURE ONLY; no model or GitHub writes.',
        completion_criteria=['Observe fixed dummy lifecycle only.'],inputs=[],write_scope=['output'],
        resource_budget={**GOAL_RESOURCE_BUDGET,'cpu_seconds_per_process':10},
        direct_pr=dict(repository=REPOSITORY,issue_number=number,base_branch='main',base_sha=INPUT_SHA,
            title='[Codex] SYNTHETIC NEVER POST',public_results_approved=True))


def worker_config():
    return dict(default_worker_pr_config(),enabled=True,actor_login=ACTOR['login'],actor_id=ACTOR['id'],
                **dict.fromkeys(CONSENTS,True))


class LocalWorkerAPI:
    def __init__(self,token):
        if token!=DUMMY_TOKEN.decode():raise ContractError('diagnostic dummy token required')
    def request(self,method,path,*args,**kwargs):
        if (method,path)!=('GET','/user'):raise ContractError('diagnostic API operation forbidden')
        return deepcopy(ACTOR)


class LocalSources:
    def __init__(self,deployment,*,worker_present=None):
        self.sources={};self.cancelled=False;self.worker_present=worker_present or self._worker_present
        expires=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat().replace('+00:00','Z')
        for number in (A,B):
            source=FixtureSource()
            request=dict(schema_version=1,plan_version=1,issuer='ken2129',
                approval_ref=f'offline-resident-{number}',expires_at=expires,cancelled=False,
                plan=plan(deployment,number))
            source.issue.update(number=number,url=f'{API}/issues/{number}',
                body='SYNTHETIC NOT A REAL ISSUE'+MARKER+json.dumps(request)+'\n```')
            approval=json.loads(source.comment['body'])
            approval.update(request_id=request['approval_ref'],issue=number,
                content_sha256=content_hash(source.issue),expires_at=expires)
            source.comment.update(id=f'IC_SYNTHETIC_{number}',body=json.dumps(approval))
            self.sources[number]=source

    @staticmethod
    def _worker_present():
        from .uid_supervisor import LinuxUIDBackend
        return any(p.uids==(10002,)*4 for p in LinuxUIDBackend().processes())

    def get_issue(self,number):
        if number not in self.sources:raise ContractError('non-fixture Issue forbidden')
        return deepcopy(self.sources[number].issue)

    def comments(self,number):
        self.get_issue(number)
        source=self.sources[number];comments=[deepcopy(source.comment)]
        if number==A and (self.cancelled or self.worker_present()):
            self.cancelled=True
            cancellation=deepcopy(source.comment);body=json.loads(cancellation['body'])
            body['decision']='cancel-execution'
            cancellation.update(id='IC_SYNTHETIC_CANCEL_A',body=json.dumps(body))
            comments.append(cancellation)
        return comments

    def list_candidates(self):raise ContractError('diagnostic Issue scan forbidden')


def receipt(number,run,attempt=1):
    return dict(schema='c3-issue-event-v1',repository=REPOSITORY,repository_id=1312915009,
        issue_number=number,event_name='issues',action='opened',actor_id=55587773,comment_id=None,
        run_id=run,run_attempt=attempt,workflow_sha=WORKFLOW,receipt_id=f'actions:1312915009:{run}')
