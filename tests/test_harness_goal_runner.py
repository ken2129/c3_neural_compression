import copy
import json
import unittest

from research_harness.goal_runner import GoalAppServerRunner, GoalRequest, GoalBlocked
from research_harness.goal_plan import GOAL_RESOURCE_BUDGET

SHA='a'*40

def plan():
    return dict(kind='codex-goal-v1',deployment_id='1'*32,source_sha=SHA,publish_result_pr=True,
        model='gpt-6.1-sol',sandbox_mode='externalSandbox',external_network='enabled',
        max_seconds=120,model_usage_approved=True,goal='Summarize the public research inputs.',
        completion_criteria=['Preserve every numeric observation.'],
        inputs=['data/input.csv'],write_scope=['output'],resource_budget=dict(GOAL_RESOURCE_BUDGET))


def event(method,**params):
    return {'method':method,'params':dict(threadId='t',turnId='u',**params)}


def frames():
    result=[{'id':1,'result':dict(codexHome='/work/codex-home',platformOs='linux',platformFamily='unix')},
        {'method':'configWarning','params':{'summary':'Optional metadata is not authority'}},
        {'id':2,'result':dict(thread=dict(id='t',ephemeral=True,cliVersion='0.160.0',cwd='/work/goal',turns=[]),
            cwd='/work/goal',model='gpt-6.1-sol',approvalPolicy='never',approvalsReviewer='user',
            instructionSources=[],sandbox={'type':'readOnly'})},
        {'id':3,'result':{'turn':dict(id='u',status='inProgress',error=None)}},
        event('turn/started',turn=dict(id='u',status='inProgress',error=None))]
    items=[]
    # Editing, a failing command, correcting the file and running it again are
    # ordinary agent work. Their text is never a command allowlist or success proof.
    for i,(kind,code) in enumerate([('fileChange',None),('commandExecution',1),
                                   ('fileChange',None),('commandExecution',0)]):
        start=dict(id=str(i),type=kind)
        done=dict(start,status='completed',exitCode=code,command='agent selected command')
        result += [event('item/started',item=start),event('item/completed',item=done)]
        items.append(done)
    final=dict(id='final',type='agentMessage',phase='final_answer',
               text=json.dumps(dict(outcome='completed',summary='Generated the requested files.')))
    result += [event('item/started',item=dict(id='final',type='agentMessage',phase='final_answer')),
               event('item/completed',item=final),
               event('turn/completed',turn=dict(id='u',status='completed',error=None,
                    itemsView='summary',items=[copy.deepcopy(final)]))]
    return result


class Transport:
    def __init__(self,values):self.values=copy.deepcopy(values);self.sent=[]
    def receive(self):return self.values.pop(0)
    def send(self,value):self.sent.append(copy.deepcopy(value))


class GoalRunnerTests(unittest.TestCase):
    def run_frames(self,values=None,**kw):
        transport=Transport(frames() if values is None else values)
        request=GoalRequest(plan(),SHA)
        result=GoalAppServerRunner(enabled=True).run(request,transport,
            admit=lambda request:None,recheck=kw.get('recheck',lambda:None))
        return result,transport

    def test_multiple_commands_failure_edit_and_repair_are_allowed(self):
        result,transport=self.run_frames()
        self.assertEqual(result.outcome,'completed')
        self.assertFalse(result.independently_validated)
        turn=next(x for x in transport.sent if x.get('method')=='turn/start')
        text=turn['params']['input'][0]['text']
        self.assertIn('Choose your own commands',text)
        self.assertNotIn('sum(i*i',text)
        self.assertNotIn('/bin/bash',text)

    def test_disabled_does_not_admit_or_send(self):
        transport=Transport([])
        with self.assertRaisesRegex(GoalBlocked,'DISABLED'):
            GoalAppServerRunner().run(GoalRequest(plan(),SHA),transport,
                admit=lambda _:self.fail(),recheck=lambda:self.fail())
        self.assertEqual(transport.sent,[])

    def test_additional_permission_is_rejected(self):
        values=frames();values.insert(5,dict(id=99,method='item/commandExecution/requestApproval',params={}))
        with self.assertRaisesRegex(GoalBlocked,'PERMISSION'):
            self.run_frames(values)

    def test_missing_or_wrong_turn_is_rejected(self):
        for turn in (None,'other'):
            values=frames()
            if turn is None:del values[5]['params']['turnId']
            else:values[5]['params']['turnId']=turn
            with self.assertRaises(GoalBlocked):self.run_frames(values)

    def test_final_snapshot_must_match_observed_final(self):
        values=frames();values[-1]['params']['turn']['items'][0]['text']='{}'
        with self.assertRaisesRegex(GoalBlocked,'SNAPSHOT'):
            self.run_frames(values)

    def test_completion_without_start_is_rejected(self):
        values=frames();del values[4]
        with self.assertRaisesRegex(GoalBlocked,'OUTSIDE_TURN'):
            self.run_frames(values)

    def test_optional_progress_fields_do_not_gate_commands(self):
        values=frames();values.insert(6,event('item/newProgressEvent',extra={'arbitrary':'bounded data'}))
        self.assertEqual(self.run_frames(values)[0].outcome,'completed')

    def test_failure_candidate_is_not_protocol_failure(self):
        values=frames()
        candidate=json.dumps({'outcome':'incomplete','summary':'Could not finish.'})
        values[-2]['params']['item']['text']=candidate
        values[-1]['params']['turn']['items'][0]['text']=candidate
        self.assertEqual(self.run_frames(values)[0].outcome,'incomplete')

    def test_changed_plan_during_admission_blocks(self):
        request=GoalRequest(plan(),SHA)
        def change(_):request.plan['goal']='Changed goal'
        with self.assertRaisesRegex(GoalBlocked,'PLAN_CHANGED'):
            GoalAppServerRunner(enabled=True).run(request,Transport([]),admit=change,recheck=lambda:None)

    def test_server_error_does_not_disclose_remote_body(self):
        values=frames();values.insert(5,{'method':'error','params':{'secret':'never report'}})
        with self.assertRaisesRegex(GoalBlocked,'GOAL_SERVER_ERROR') as raised:self.run_frames(values)
        self.assertNotIn('secret',str(raised.exception))

if __name__=='__main__':unittest.main()
