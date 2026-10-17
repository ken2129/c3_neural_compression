"""Private durable controller state. Explicit initialization, no automatic repair."""
import hashlib
import os
from pathlib import Path
import sqlite3
import uuid

from .issue_intake import Intake, MAX_TRACKED
from .store import SingleInstance
from .persistent_fixture import private, exclusive_record, sync_directory, EXPECTED_SQL
from .schema import ContractError, canonical, loads, fields, identifier

TABLES={key:EXPECTED_SQL[key] for key in ('intake','owner_receipts','owner_approval_blocks')}
TABLES.update({
 'service_epochs':'CREATE TABLE service_epochs (issue INTEGER PRIMARY KEY, before_epoch TEXT NOT NULL, after_epoch TEXT)',
 'service_meta':'CREATE TABLE service_meta (nonce TEXT PRIMARY KEY NOT NULL)',
 'service_runs':'CREATE TABLE service_runs (issue INTEGER PRIMARY KEY, binding TEXT NOT NULL, phase TEXT NOT NULL, result TEXT, publication TEXT, notification TEXT NOT NULL)',
})
PHASES={'CLAIMED','BLOCKED','INTERRUPTED','RESULT_AWAITING_RESTART','RESULT_READY','PUBLISHED'}


def validate_binding(value):
    fields(value, ('issue_number','issue_content_sha256','request_id','run_id','plan_digest','source_sha'))
    if type(value['issue_number']) is not int or value['issue_number']<=0 or value['issue_number']==56:
        raise ContractError('invalid run issue')
    identifier(value['request_id']);identifier(value['run_id'])
    for key,size in (('issue_content_sha256',64),('plan_digest',64),('source_sha',40)):
        raw=value[key]
        if type(raw) is not str or len(raw)!=size or any(c not in '0123456789abcdef' for c in raw):
            raise ContractError('invalid binding digest')


def validate_request(request,value):
    if request.issue_content_sha256!=value['issue_content_sha256'] or request.base_sha!=value['source_sha']:
        raise ContractError('saved publication binding mismatch')
    for key in ('issue_number','request_id','run_id','plan_digest','source_sha'):
        if getattr(request.result,key)!=value[key]:raise ContractError('saved result binding mismatch')


def initialize(root):
    root=Path(root);private(root,True)
    if {p.name for p in root.iterdir()} not in (set(),{'manager.lock'}):
        raise ContractError('service initialization requires empty dedicated directory')
    if (root/'manager.lock').exists():private(root/'manager.lock')
    nonce=uuid.uuid4().hex
    exclusive_record(root/'initialized.json',{'version':1,'nonce':nonce})
    old=os.umask(0o077)
    try:
        (root/'claims').mkdir(mode=0o700)
        db=sqlite3.connect(root/'state.sqlite')
        try:
            for sql in TABLES.values():db.execute(sql)
            db.execute('INSERT INTO service_meta VALUES (?)',(nonce,))
            db.execute('PRAGMA user_version=1');db.commit()
        finally:db.close()
        sync_directory(root)
    finally:os.umask(old)


class ServiceState:
    def __init__(self,root,*,require_epochs=False):
        if type(require_epochs) is not bool:raise ContractError('invalid epoch policy')
        self.require_epochs=require_epochs
        self.root=Path(root);private(self.root,True)
        private(self.root/'claims',True)
        for name in ('state.sqlite','initialized.json'):private(self.root/name)
        marker=loads((self.root/'initialized.json').read_bytes())
        if type(marker) is not dict or set(marker)!={'version','nonce'} or marker['version']!=1:
            raise ContractError('unsupported service initialization')
        self._lock=SingleInstance(self.root/'manager.lock')
        self._lock.__enter__()
        try:
            private(self.root/'manager.lock')
            self.db=sqlite3.connect((self.root/'state.sqlite').as_uri()+'?mode=rw',uri=True)
        except BaseException:
            self._lock.__exit__(None,None,None);raise
        try:
            if self.db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or self.db.execute('PRAGMA user_version').fetchone()!=(1,):
                raise ContractError('service database invalid')
            actual=dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            if actual!=TABLES:raise ContractError('unknown service schema')
            if self.db.execute('SELECT nonce FROM service_meta').fetchall()!=[(marker['nonce'],)]:
                raise ContractError('service volume identity mismatch')
            self.intake=Intake.__new__(Intake);self.intake.db=self.db
            self._validate_runs()
        except BaseException:
            self.db.close();self._lock.__exit__(None,None,None);raise

    def _validate_runs(self):
        rows=self.db.execute('SELECT issue,binding,phase,result,publication,notification FROM service_runs').fetchall()
        files={p.name for p in (self.root/'claims').iterdir()}
        if files!={str(r[0])+'.json' for r in rows}:
            raise ContractError('claim witness and database disagree')
        epoch_rows={r[0]:r[1:] for r in self.db.execute('SELECT issue,before_epoch,after_epoch FROM service_epochs')}
        if set(epoch_rows)-{r[0] for r in rows}:raise ContractError('orphan runtime epoch')
        if self.require_epochs and set(epoch_rows)!={r[0] for r in rows}:raise ContractError('live run is missing runtime epoch')
        for issue,binding,phase,result,publication,notification in rows:
            path=self.root/'claims'/(str(issue)+'.json');private(path)
            if path.read_bytes()!=binding.encode() or phase not in PHASES or notification!='UNCONFIGURED':
                raise ContractError('invalid saved run state')
            value=loads(binding);validate_binding(value)
            if value['issue_number']!=issue:raise ContractError('saved run identity mismatch')
            frozen=self.db.execute('SELECT content_hash FROM owner_receipts WHERE issue=?',(issue,)).fetchone()
            if frozen!=(value['issue_content_sha256'],):raise ContractError('saved approval missing')
            observed=self.db.execute('SELECT fingerprint FROM intake WHERE number=?',(issue,)).fetchone()
            if observed!=(value['issue_content_sha256'],):raise ContractError('saved observation missing or changed')
            if phase in ('RESULT_AWAITING_RESTART','RESULT_READY','PUBLISHED'):
                if result is None:raise ContractError('saved result missing')
                # Validate the closed, independently checked artifact on every reopen.
                from .result_publisher import parse_publication_request
                request=parse_publication_request(loads(result))
                validate_request(request,value)
            elif result is not None or publication is not None:
                raise ContractError('unfinished run has publication data')
            if phase=='RESULT_AWAITING_RESTART' and publication is not None:raise ContractError('publication precedes restart boundary')
            if issue in epoch_rows:
                from .restart_boundary import RestartEpoch, restart_boundary_candidate
                before,after=epoch_rows[issue]
                prior=RestartEpoch.from_dict(loads(before))
                if phase in ('INTERRUPTED','RESULT_READY','PUBLISHED'):
                    if after is None:raise ContractError('missing runtime restart confirmation')
                    if not restart_boundary_candidate(prior,RestartEpoch.from_dict(loads(after)),expected_container_id=prior.container_id,deployment_contract_verified=True):
                        raise ContractError('saved restart metadata mismatch')
                elif after is not None:raise ContractError('premature runtime restart confirmation')
            elif phase in ('INTERRUPTED','RESULT_AWAITING_RESTART'):raise ContractError('missing prior runtime epoch')
            if publication is not None:
                from .result_publisher import PublicationState
                state=PublicationState.from_dict(loads(publication))
                if state.request_digest!=request.digest or state.branch!=request.branch:raise ContractError('saved publisher state mismatch')
            if phase=='PUBLISHED' and (publication is None or state.phase!='PUBLISHED'):
                raise ContractError('published run missing verified PR state')

    def close(self):
        self.db.close();self._lock.__exit__(None,None,None)

    def get(self,issue):
        row=self.db.execute('SELECT binding,phase,result,publication FROM service_runs WHERE issue=?',(issue,)).fetchone()
        return None if row is None else dict(binding=loads(row[0]),phase=row[1],result=loads(row[2]) if row[2] else None,publication=loads(row[3]) if row[3] else None)

    def pending_claims(self):
        """Bounded local recovery inventory; never consult or change intake."""
        rows=self.db.execute("SELECT issue FROM service_runs WHERE phase='CLAIMED' ORDER BY issue LIMIT ?",(MAX_TRACKED+1,)).fetchall()
        if len(rows)>MAX_TRACKED:raise ContractError('pending claim limit reached')
        return [row[0] for row in rows]

    def claim(self,binding,epoch=None):
        validate_binding(binding)
        if self.require_epochs and epoch is None:raise ContractError('live claim requires runtime epoch')
        if epoch is not None:
            from .restart_boundary import RestartEpoch
            if type(epoch) is not RestartEpoch:raise ContractError('verified runtime epoch type required')
        issue=binding['issue_number']
        frozen=self.db.execute('SELECT content_hash FROM owner_receipts WHERE issue=?',(issue,)).fetchone()
        if frozen!=(binding['issue_content_sha256'],):raise ContractError('claim requires frozen approval')
        observed=self.db.execute('SELECT fingerprint FROM intake WHERE number=?',(issue,)).fetchone()
        if observed!=(binding['issue_content_sha256'],):raise ContractError('claim requires matching observation')
        previous=self.get(issue)
        if previous is not None:
            if previous['binding']!=binding:raise ContractError('existing claim binding changed')
            return False
        encoded=canonical(binding).decode()
        # A crash between witness creation and DB commit blocks startup, not retry.
        exclusive_record(self.root/'claims'/(str(issue)+'.json'),binding)
        self.db.execute('INSERT INTO service_runs VALUES (?,?,?,NULL,NULL,?)',(issue,encoded,'CLAIMED','UNCONFIGURED'))
        if epoch is not None:
            from .restart_boundary import RestartEpoch
            if type(epoch) is not RestartEpoch:raise ContractError('verified runtime epoch type required')
            self.db.execute('INSERT INTO service_epochs VALUES (?,?,NULL)',(issue,canonical(epoch.to_dict()).decode()))
        self.db.commit();return True

    def block(self,issue):
        self.db.execute("UPDATE service_runs SET phase='BLOCKED' WHERE issue=? AND phase='CLAIMED'",(issue,));self.db.commit()

    def interrupt_after_restart(self,issue,epoch,*,deployment_contract_verified=False):
        """Record a prior claimed generation ending without a validated result.

        This is terminal, with unknown execution outcome. It never releases the
        claim or reconstructs a result. Reopening state is not restart evidence;
        the trusted caller must supply a new epoch under the verified container
        contract. Both epochs and the original claim witness remain durable.
        """
        from .restart_boundary import RestartEpoch, restart_boundary_candidate
        old=self.get(issue)
        if old is None or old['phase']!='CLAIMED':raise ContractError('no claim awaiting interruption check')
        row=self.db.execute('SELECT before_epoch,after_epoch FROM service_epochs WHERE issue=?',(issue,)).fetchone()
        if row is None:return False
        if row[1] is not None:raise ContractError('invalid pending epoch')
        prior=RestartEpoch.from_dict(loads(row[0]))
        if not restart_boundary_candidate(prior,epoch,expected_container_id=prior.container_id,
                                          deployment_contract_verified=deployment_contract_verified):
            return False
        # Commit the new epoch and terminal phase together, or retain CLAIMED.
        with self.db:
            self.db.execute('UPDATE service_epochs SET after_epoch=? WHERE issue=?',(canonical(epoch.to_dict()).decode(),issue))
            cur=self.db.execute("UPDATE service_runs SET phase='INTERRUPTED' WHERE issue=? AND phase='CLAIMED' AND result IS NULL AND publication IS NULL",(issue,))
            if cur.rowcount!=1:raise ContractError('interruption requires an unfinished claim')
        return True

    def save_result(self,issue,request,await_restart=False):
        old=self.get(issue)
        if old is None:raise ContractError('result requires existing claim')
        validate_request(request,old['binding'])
        has_epoch=self.db.execute('SELECT issue FROM service_epochs WHERE issue=?',(issue,)).fetchone() is not None
        if type(await_restart) is not bool or has_epoch!=await_restart:raise ContractError('restart result boundary required')
        phase='RESULT_AWAITING_RESTART' if await_restart else 'RESULT_READY'
        cur=self.db.execute("UPDATE service_runs SET phase=?,result=? WHERE issue=? AND phase='CLAIMED'",(phase,canonical(request.to_dict()).decode(),issue))
        if cur.rowcount!=1:raise ContractError('result requires existing claim')
        self.db.commit()

    def confirm_restart(self,issue,epoch,*,deployment_contract_verified=False):
        from .restart_boundary import RestartEpoch, restart_boundary_candidate
        old=self.get(issue)
        if old is None or old['phase']!='RESULT_AWAITING_RESTART':raise ContractError('no result awaiting restart')
        row=self.db.execute('SELECT before_epoch,after_epoch FROM service_epochs WHERE issue=?',(issue,)).fetchone()
        if row is None or row[1] is not None:raise ContractError('invalid pending epoch')
        prior=RestartEpoch.from_dict(loads(row[0]))
        if not restart_boundary_candidate(prior,epoch,expected_container_id=prior.container_id,
                                          deployment_contract_verified=deployment_contract_verified):
            raise ContractError('restart deployment boundary is not verified')
        self.db.execute('UPDATE service_epochs SET after_epoch=? WHERE issue=?',(canonical(epoch.to_dict()).decode(),issue))
        self.db.execute("UPDATE service_runs SET phase='RESULT_READY' WHERE issue=?",(issue,))
        self.db.commit()

    def save_publication(self,issue,state):
        from .result_publisher import parse_publication_request
        old=self.get(issue)
        if old is None or old['result'] is None:raise ContractError('publication requires saved result')
        request=parse_publication_request(old['result'])
        if state.request_digest!=request.digest or state.branch!=request.branch:raise ContractError('publication identity mismatch')
        phase='PUBLISHED' if state.phase=='PUBLISHED' else 'RESULT_READY'
        self.db.execute('UPDATE service_runs SET phase=?,publication=? WHERE issue=? AND phase IN (?,?)',
                        (phase,canonical(state.to_dict()).decode(),issue,'RESULT_READY','PUBLISHED'))
        self.db.commit()
