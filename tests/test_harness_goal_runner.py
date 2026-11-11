import copy
import json
import unittest

from research_harness.goal_runner import (GoalAppServerRunner, GoalRequest, GoalBlocked,
                                         safe_goal_diagnostic)
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

    def test_warning_after_tools_allows_global_or_matching_optional_scope(self):
        scopes=({}, {'threadId':None}, {'threadId':None,'turnId':None}, {'threadId':'t'},
                {'threadId':'t','turnId':None}, {'threadId':'t','turnId':'u'})
        for scope in scopes:
            with self.subTest(scope=scope):
                values=frames()
                values.insert(-3,{'method':'warning','params':dict(
                    message='Optional bounded metadata.',**scope)})
                result,transport=self.run_frames(values)
                self.assertEqual(result.outcome,'completed')
                self.assertEqual(transport.values,[])

    def test_global_warning_is_metadata_in_every_protocol_phase(self):
        for index in (0,2,3,4,-3,-1):
            for scope in ({},{'threadId':None}):
                with self.subTest(index=index,scope=scope):
                    values=frames()
                    values.insert(index,{'method':'warning','params':dict(
                        message='Optional bounded metadata.',**scope)})
                    self.assertEqual(self.run_frames(values)[0].outcome,'completed')

    def test_thread_scoped_warning_needs_no_turn_id_or_started_turn(self):
        for index in (2,3,4,-3,-1):
            with self.subTest(index=index):
                values=frames()
                values.insert(index,{'method':'warning','params':{
                    'message':'Optional bounded metadata.','threadId':'t'}})
                self.assertEqual(self.run_frames(values)[0].outcome,'completed')

    def test_warning_with_explicit_wrong_scope_is_rejected(self):
        cases=[({'threadId':'other'},'GOAL_THREAD_MISMATCH'),
               ({'threadId':'t','turnId':'other'},'GOAL_TURN_MISMATCH')]
        for scope,reason in cases:
            with self.subTest(scope=scope):
                values=frames()
                values.insert(-3,{'method':'warning','params':dict(
                    message='Optional bounded metadata.',**scope)})
                with self.assertRaisesRegex(GoalBlocked,reason):self.run_frames(values)

    def test_warning_requires_a_string_message(self):
        for params in ({},{'message':None},{'message':False},{'message':17},
                       {'message':{}},{'message':[]}):
            for index in (0,2,3,-3):
                with self.subTest(params=params,index=index):
                    values=frames();values.insert(index,{'method':'warning','params':params})
                    with self.assertRaisesRegex(GoalBlocked,'GOAL_NOTIFICATION_ENVELOPE'):
                        self.run_frames(values)

    def test_warning_rejects_malformed_explicit_scope_identifiers(self):
        for field in ('threadId','turnId'):
            for value in (False,17,'','bad\nidentifier','x'*257,{},[]):
                with self.subTest(field=field,value=value):
                    params={'message':'Optional bounded metadata.',field:value}
                    values=frames();values.insert(-3,{'method':'warning','params':params})
                    with self.assertRaisesRegex(GoalBlocked,'GOAL_PROTOCOL_IDENTIFIER'):
                        self.run_frames(values)

    def test_warning_cannot_replace_the_final_response(self):
        values=frames()
        final_text=values[-2]['params']['item']['text']
        values[-3:-1]=[{'method':'warning','params':{'message':final_text}}]
        with self.assertRaisesRegex(GoalBlocked,'GOAL_FINAL_RESPONSE_MISSING'):
            self.run_frames(values)

    def test_missing_or_wrong_thread_on_item_or_turn_is_rejected(self):
        for index in (4,5,6,-2,-1):
            for thread in ('missing',None,'other'):
                with self.subTest(method=frames()[index]['method'],thread=thread):
                    values=frames()
                    if thread == 'missing':del values[index]['params']['threadId']
                    else:values[index]['params']['threadId']=thread
                    with self.assertRaisesRegex(GoalBlocked,'GOAL_THREAD_MISMATCH'):
                        self.run_frames(values)

    def test_threadless_unknown_notification_is_not_treated_as_warning(self):
        values=frames()
        values.insert(-3,{'method':'warning/newProgressEvent','params':{
            'message':'Optional bounded metadata.'}})
        with self.assertRaisesRegex(GoalBlocked,'GOAL_THREAD_MISMATCH'):
            self.run_frames(values)

    def test_warning_does_not_allow_additional_permission_requests(self):
        values=frames()
        values[-3:-3]=[{'method':'warning','params':{'message':'Optional bounded metadata.'}},
            {'id':99,'method':'item/commandExecution/requestApproval','params':{}}]
        with self.assertRaisesRegex(GoalBlocked,'GOAL_ADDITIONAL_PERMISSION_REQUEST'):
            self.run_frames(values)

    def test_id_bearing_warning_is_still_an_unapproved_server_request(self):
        for index in (0,2,3,-3):
            with self.subTest(index=index):
                values=frames()
                values.insert(index,{'id':99,'method':'warning','params':{
                    'message':'Optional bounded metadata.'}})
                with self.assertRaisesRegex(GoalBlocked,'GOAL_ADDITIONAL_PERMISSION_REQUEST'):
                    self.run_frames(values)

    def test_pending_event_failure_diagnostic_identifies_the_notification(self):
        cases=[(0,'GOAL_PREINITIALIZATION_ACTIVITY','initialize_validate',False),
               (2,'GOAL_PRETURN_ACTIVITY','thread_start_validate',False),
               (3,'GOAL_THREAD_MISMATCH','stream',True)]
        for index,reason,phase,turn_accepted in cases:
            with self.subTest(index=index):
                values=frames()
                pending=event('item/started',item={'id':'pending','type':'commandExecution'})
                pending['params']['threadId']='other'
                values.insert(index,pending)  # Notification is queued before an RPC response.
                with self.assertRaisesRegex(GoalBlocked,reason) as raised:
                    self.run_frames(values)
                protocol=safe_goal_diagnostic(raised.exception)['protocol']
                self.assertEqual((protocol['phase'],protocol['frame_kind'],protocol['method']),
                                 (phase,'notification','item/started'))
                self.assertEqual(protocol['turn_accepted'],turn_accepted)
                self.assertFalse(protocol['final_validated'])

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
