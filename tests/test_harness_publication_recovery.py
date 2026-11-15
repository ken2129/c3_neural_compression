"""Fake-only saved-result publication recovery; no gh, model or host operation."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from research_harness.issue_intake import content_hash
from research_harness.model_result import ModelCanaryResult
from research_harness.owner_approval import OWNER_ID, OWNER_LOGIN
from research_harness.publication_recovery import (recover_publication, validate_fence,
    _original_files, ENTRYPOINT)
from research_harness.restart_boundary import RestartEpoch
from research_harness.result_publisher import ModelPublicationRequest, PublicationState
from research_harness.schema import ContractError, canonical, loads
from research_harness.service_state import ServiceState, initialize
from research_harness.store import ResourceBusy
from test_harness_result_publisher import FakeTransport

NOW = 1792242000.0
OLD_ID, NEW_ID = '1'*64, '2'*64
SOURCE, NEW_BASE, RECOVERY_SOURCE = 'a'*40, 'b'*40, 'c'*40

def stamp(t): return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')

def fence_fixture():
    mount = dict(Type='volume', Name='original-volume', Destination='/control', RW=True,
                 Source='/var/lib/docker/volumes/original-volume/_data')
    return dict(schema='c3-publication-recovery-deployment-v1', mode='prepare', source_sha=RECOVERY_SOURCE,
        observed_at=stamp(NOW-1), expires_at=stamp(NOW+3500),
        old=dict(Id=OLD_ID, Config=dict(Hostname=OLD_ID[:12], Labels={
            'c3.purpose':'dedicated-research-controller', 'c3.source-sha':SOURCE}),
            State=dict(Status='exited', Running=False, Restarting=False, Paused=False, Dead=False, Pid=0),
            HostConfig=dict(RestartPolicy={'Name':'no'}), Mounts=[deepcopy(mount)]),
        recovery=dict(Id=NEW_ID, Image='sha256:'+'d'*64,
            Config=dict(User='10001:10001', Hostname=NEW_ID[:12], Entrypoint=ENTRYPOINT,
                        Cmd=['--once'], Labels={'c3.purpose':'publication-only-recovery',
                                                 'c3.source-sha':RECOVERY_SOURCE}),
            HostConfig=dict(RestartPolicy={'Name':'no'}, Privileged=False, ReadonlyRootfs=True,
                Init=False,
                PidMode='', IpcMode='private', CgroupnsMode='private', NetworkMode='bridge',
                CapAdd=[], CapDrop=['ALL'], SecurityOpt=['no-new-privileges:true'], Runtime='runc',
                Devices=[], DeviceRequests=[], DeviceCgroupRules=[], Tmpfs={}, Memory=1024**3,
                MemorySwap=1024**3, PidsLimit=64, NanoCpus=10**9, Binds=['original-volume:/control:rw']),
            Mounts=[deepcopy(mount)]),
        consumers=sorted([OLD_ID,NEW_ID]), volume=dict(Name='original-volume', Driver='local',
            Scope='local', Options={}, Mountpoint=mount['Source']))


class RecoveryTransport(FakeTransport):
    def branch_sha(self, request):
        return None if request.base_sha == SOURCE else super().branch_sha(request)
    def find_pr(self, request):
        return None if request.base_sha == SOURCE else super().find_pr(request)


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.control = Path(self.tmp.name); self.control.chmod(0o700)
        self.root = self.control/'state'; self.root.mkdir(mode=0o700); initialize(self.root)
        self.journal = self.control/'publication-recovery'
        self.issue = dict(number=77, author_id=OWNER_ID, title='original approved issue', body='original plan',
                          state='open', updated_at=stamp(NOW-100), is_pr=False)
        self.binding = dict(issue_number=77, issue_content_sha256=content_hash(self.issue),
            request_id='request-77', run_id='run-77', plan_digest='e'*64, source_sha=SOURCE)
        result = ModelCanaryResult(**{k:v for k,v in self.binding.items() if k!='issue_content_sha256'},
            model='pinned-model', reported_outcome='success', reported_checksum=333283335000,
            command_observed=True, elapsed_seconds=1.25)
        self.original = ModelPublicationRequest(result, SOURCE, self.binding['issue_content_sha256'])
        state = ServiceState(self.root)
        state.db.execute('INSERT INTO intake VALUES (?,?,?,?,?,?)',
            (77,self.binding['issue_content_sha256'],'PENDING_TRUSTED_APPROVAL',0,1,'PENDING_TRUSTED_APPROVAL'))
        state.db.execute('INSERT INTO owner_receipts VALUES (?,?,?,?)',
            (77,'old-owner-receipt','e'*64,self.binding['issue_content_sha256'])); state.db.commit()
        before = RestartEpoch(OLD_ID,'12345678-1234-1234-1234-123456789abc',11,100)
        state.claim(self.binding,before)
        state.save_result(77,self.original,await_restart=True)
        state.confirm_restart(77,replace(before,pid1_start_ticks=101),deployment_contract_verified=True)
        state.save_publication(77,PublicationState(self.original.digest,self.original.branch,
            phase='BLOCKED',reason='BASE_CHANGED'))
        state.close()
        self.original_files = _original_files(self.root)
        self.transport = RecoveryTransport(replace(self.original,base_sha=NEW_BASE))
        self.issue_client = Mock(); self.issue_client.get_issue.side_effect=lambda number:deepcopy(self.issue)
        self.comments_client = Mock(); self.comments_client.comments.return_value=[]
        self.fence = fence_fixture()
        prepared = self.run_recovery()
        body = prepared['approval_template']; body['expires_at']=stamp(NOW+600)
        self.comment = dict(id='IC_PUBLICATION_ONLY',body=json.dumps(body),
            createdAt=stamp(NOW-1),updatedAt=stamp(NOW-1),lastEditedAt=None,editor=None,isMinimized=False,
            author={'databaseId':OWNER_ID,'login':OWNER_LOGIN})
        self.comments_client.comments.return_value=[self.comment]

    def run_recovery(self, publish=False, now=NOW):
        self.fence['mode']='publish' if publish else 'prepare'
        return recover_publication(self.root,self.journal,fence=self.fence,source_sha=RECOVERY_SOURCE,
            transport=self.transport,issue_client=self.issue_client,comments_client=self.comments_client,
            publish=publish,clock=lambda:now)

    def test_prepare_is_read_only_and_binds_exact_new_envelope(self):
        answer=self.run_recovery()
        self.assertEqual(answer['phase'],'PREPARED_PUBLICATION_ONLY')
        self.assertFalse(self.journal.exists()); self.assertEqual(self.transport.events,[])
        self.assertEqual(_original_files(self.root),self.original_files)
        new=replace(self.original,base_sha=NEW_BASE)
        self.assertEqual(answer['new_request_digest'],new.digest)
        self.assertEqual(answer['original_request_digest'],self.original.digest)
        self.assertEqual(new.result,self.original.result)
        self.assertEqual(new.result.source_sha,SOURCE)
        self.assertEqual(answer['result']['reported_checksum'],333283335000)
        self.assertFalse(answer['result']['research_success_verified'])

    def test_publish_separate_journal_without_modifying_original_or_running(self):
        answer=self.run_recovery(True)
        self.assertEqual(answer['phase'],'PUBLISHED')
        self.assertEqual(self.transport.events,['branch','content','pr'])
        self.assertEqual(_original_files(self.root),self.original_files)
        journal=loads((self.journal/'journal.json').read_bytes())
        self.assertEqual(journal['binding']['request']['result'],self.original.result.to_dict())
        self.assertEqual(journal['publication']['phase'],'PUBLISHED')
        self.assertEqual(self.comments_client.comments.call_count,4)
        state=ServiceState(self.root,require_epochs=True)
        try:
            self.assertEqual(state.get(77)['phase'],'RESULT_READY')
            self.assertEqual(state.get(77)['result'],self.original.to_dict())
            self.assertEqual(state.get(77)['publication']['reason'],'BASE_CHANGED')
        finally:state.close()

    def test_original_manager_lock_is_held_and_never_stolen(self):
        state=ServiceState(self.root)
        try:
            with self.assertRaises(ResourceBusy):self.run_recovery(True)
        finally:state.close()
        self.assertEqual(self.transport.events,[])

    def test_legacy_source_base_mismatch_cannot_be_recovered(self):
        state=ServiceState(self.root)
        try:
            changed=replace(self.original,base_sha='f'*40)
            state.db.execute('UPDATE service_runs SET result=?,publication=? WHERE issue=77',
                (canonical(changed.to_dict()).decode(),canonical(PublicationState(changed.digest,
                    changed.branch,phase='BLOCKED',reason='BASE_CHANGED').to_dict()).decode()))
            state.db.commit()
        finally:state.close()
        with self.assertRaises(ContractError):self.run_recovery(True)
        self.assertEqual(self.transport.events,[])

    def test_unknown_or_partially_written_original_publication_is_ineligible(self):
        for phase,head,reason in (('BRANCH_UNKNOWN',None,None),
                                 ('BLOCKED','f'*40,'BASE_CHANGED'),
                                 ('BLOCKED',None,'REMOTE_CONFLICT')):
            original=self.original.to_dict()
            publication=PublicationState(self.original.digest,self.original.branch,
                phase=phase,head_sha=head,reason=reason)
            with sqlite3.connect(self.root/'state.sqlite') as db:
                db.execute('UPDATE service_runs SET publication=? WHERE issue=77',
                           (canonical(publication.to_dict()).decode(),))
            with self.subTest(phase=phase,head=head),self.assertRaises(ContractError):
                self.run_recovery(True)
            self.assertEqual(self.transport.events,[])

    def test_missing_restart_or_lock_is_not_reconstructed(self):
        with sqlite3.connect(self.root/'state.sqlite') as db:
            db.execute('UPDATE service_epochs SET after_epoch=NULL WHERE issue=77')
        with self.assertRaises(ContractError):self.run_recovery(True)
        (self.root/'manager.lock').unlink()
        with self.assertRaises(FileNotFoundError):self.run_recovery(True)
        self.assertFalse((self.root/'manager.lock').exists())
        self.assertEqual(self.transport.events,[])

    def test_expired_edited_forged_or_changed_receipt_never_writes(self):
        original=deepcopy(self.comment)
        for mutation in ('absent','author','edited','expiry','newbase','newdigest','decision','duplicate'):
            self.comment=deepcopy(original)
            if mutation=='absent':comments=[]
            elif mutation=='duplicate':comments=[self.comment,self.comment]
            else:
                if mutation=='author':self.comment['author']['databaseId']=1
                elif mutation=='edited':self.comment['lastEditedAt']=stamp(NOW)
                else:
                    body=json.loads(self.comment['body'])
                    body[{'expiry':'expires_at','newbase':'new_base_sha','newdigest':'new_request_digest',
                          'decision':'decision'}[mutation]]={'expiry':stamp(NOW),'newbase':'f'*40,
                          'newdigest':'f'*64,'decision':'approve-execution'}[mutation]
                    self.comment['body']=json.dumps(body)
                comments=[self.comment]
            self.comments_client.comments.return_value=comments
            with self.subTest(mutation=mutation),self.assertRaises(ContractError):self.run_recovery(True)
            self.assertEqual(self.transport.events,[]);self.assertFalse(self.journal.exists())

    def test_fence_rejects_running_restart_or_extra_volume_consumer(self):
        mutations=[lambda f:f['old']['State'].update(Running=True),
            lambda f:f['old']['HostConfig']['RestartPolicy'].update(Name='unless-stopped'),
            lambda f:f['consumers'].append('3'*64),
            lambda f:f['recovery']['HostConfig'].update(Privileged=True),
            lambda f:f['old']['Config']['Labels'].update({'c3.source-sha':'f'*40}),
            lambda f:f['recovery']['Config'].update(User='0'),
            lambda f:f.update(expires_at=stamp(NOW)),
            lambda f:f.update(mode='automatic'),lambda f:f.pop('mode'),
            lambda f:f['recovery']['Config'].update(Cmd=['--publish'])]
        for mutate in mutations:
            f=fence_fixture();mutate(f)
            with self.subTest(mutate=mutate),self.assertRaises(ContractError):
                validate_fence(f,old_container_id=OLD_ID,original_source_sha=SOURCE,
                               source_sha=RECOVERY_SOURCE,now=NOW)

    def test_optional_empty_docker_fields_may_be_omitted_but_not_nonempty(self):
        f=fence_fixture()
        optional=('CapAdd','Devices','DeviceRequests','DeviceCgroupRules','Tmpfs','Binds')
        for key in optional:f['recovery']['HostConfig'].pop(key)
        f['volume'].pop('Options')
        self.assertEqual(validate_fence(f,old_container_id=OLD_ID,original_source_sha=SOURCE,
            source_sha=RECOVERY_SOURCE,now=NOW),NEW_ID)
        for key in optional:
            bad=deepcopy(f);bad['recovery']['HostConfig'][key]=['unexpected']
            with self.subTest(key=key),self.assertRaises(ContractError):
                validate_fence(bad,old_container_id=OLD_ID,original_source_sha=SOURCE,
                    source_sha=RECOVERY_SOURCE,now=NOW)

    def test_unknown_write_is_not_repeated_after_absence_or_expired_approval(self):
        self.transport.modes['branch']='unknown_before'
        self.assertEqual(self.run_recovery(True)['phase'],'BRANCH_UNKNOWN')
        self.assertEqual(self.transport.events,['branch'])
        self.comments_client.comments.return_value=[]
        self.assertEqual(self.run_recovery(True,now=NOW+601)['phase'],'BRANCH_UNKNOWN')
        self.assertEqual(self.transport.events,['branch'])
        self.assertEqual(_original_files(self.root),self.original_files)

    def test_revocation_between_writes_blocks_remaining_operations(self):
        self.comments_client.comments.side_effect=[[self.comment],[self.comment],[]]
        answer=self.run_recovery(True)
        self.assertEqual(answer['phase'],'BLOCKED')
        self.assertEqual(answer['reason'],'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.events,['branch'])

    def test_issue_change_and_old_remote_branch_block(self):
        self.issue['body']='changed'
        with self.assertRaises(ContractError):self.run_recovery(True)
        self.issue['body']='original plan'
        self.transport.branch_sha=Mock(return_value=SOURCE)
        with self.assertRaises(ContractError):self.run_recovery(True)
        self.assertEqual(self.transport.events,[])

    def test_partial_journal_or_new_base_never_resets_unknown(self):
        self.transport.modes['branch']='unknown_before';self.run_recovery(True)
        original=(self.journal/'journal.json').read_bytes()
        self.transport.base='f'*40
        with self.assertRaises(ContractError):self.run_recovery(True)
        self.transport.base=NEW_BASE
        (self.journal/'pending.json').write_text('{}')
        with self.assertRaises(ContractError):self.run_recovery(True)
        self.assertEqual((self.journal/'journal.json').read_bytes(),original)
        self.assertEqual(self.transport.events,['branch'])

    def test_journal_symlink_is_rejected_without_remote_write(self):
        other=self.control/'other';other.mkdir(mode=0o700)
        self.journal.symlink_to(other,target_is_directory=True)
        with self.assertRaises(ContractError):self.run_recovery(True)
        self.assertEqual(list(other.iterdir()),[])
        self.assertEqual(self.transport.events,[])

    def test_unknown_applied_branch_with_deleted_approval_never_writes_content(self):
        self.transport.modes['branch']='unknown_before'
        self.assertEqual(self.run_recovery(True)['phase'],'BRANCH_UNKNOWN')
        self.transport.head=NEW_BASE
        self.comments_client.comments.return_value=[]
        answer=self.run_recovery(True)
        self.assertEqual(answer['phase'],'BLOCKED')
        self.assertEqual(answer['reason'],'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.events,['branch'])

    def test_journal_intent_is_durable_before_each_write(self):
        original_write=self.transport._write
        expected={'branch':'BRANCH_UNKNOWN','content':'CONTENT_UNKNOWN','pr':'PR_UNKNOWN'}
        def write(name, apply):
            record=loads((self.journal/'journal.json').read_bytes())
            self.assertEqual(record['publication']['phase'],expected[name])
            return original_write(name,apply)
        self.transport._write=write
        self.assertEqual(self.run_recovery(True)['phase'],'PUBLISHED')


if __name__=='__main__':unittest.main()
