"""Trusted local Docker access, outside Codex. Every operation uses a pinned ID."""
import io
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile

from .execution_contract import DOCKER_SOCKET,RUNTIME_ROOT
from .schema import ContractError


class ExecutionCleanupUnverified(ContractError):
    def __init__(self):super().__init__('EXECUTION_CLEANUP_UNVERIFIED')


def validate_container(data,target,*,created=False,deployment_id=None,alias=None):
    try:
        ident=data['Id'];host=data['HostConfig'];config=data['Config']
        if re.fullmatch('[0-9a-f]{64}',ident) is None or data['Name']!='/'+target['name']:raise ValueError()
        if target['container_id'] is not None and ident!=target['container_id']:raise ValueError()
        if (host['Privileged'] or host['PidMode']!='' or host['IpcMode']!='private'
                or host['NetworkMode'] not in ('bridge','none')):raise ValueError()
        if host.get('CapAdd') and any(c.removeprefix('CAP_') in ('SYS_ADMIN','SYS_PTRACE') for c in host['CapAdd']):raise ValueError()
        # Do not place model code in containers with host bind mounts or Docker
        # authority. Existing private data must be copied or use reviewed volumes.
        if any(m['Type']=='bind' or m['Destination'] in ('/control','/runner','/run/c3-docker.sock','/var/run/docker.sock')
                or str(m.get('Name','')).endswith(('_goal-control','_runner-state')) for m in data['Mounts']):raise ValueError()
        if host.get('DeviceCgroupRules') not in (None,[]):raise ValueError()
        if host.get('Devices') not in (None,[]):raise ValueError()
        if created:
            labels=config.get('Labels') or {}
            if (labels.get('c3.execution-deployment')!=deployment_id or labels.get('c3.execution-alias')!=alias
                    or data['Image']!=target['create_image']):raise ValueError()
            budget=target['resource_limits']
            if (host['Memory']!=budget['memory_bytes'] or host['MemorySwap']!=budget['memory_bytes']
                    or host['PidsLimit']!=budget['max_processes'] or host['NanoCpus']!=target['cpu_cores']*10**9):raise ValueError()
        if not target['sampled_limits_accepted'] and not created:raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError):
        raise ContractError('execution container differs from registered identity/policy') from None
    return ident


def validate_docker_authority():
    info=Path(DOCKER_SOCKET).lstat()
    binary=Path('/usr/bin/docker').stat()
    if (os.geteuid()!=0 or not stat.S_ISSOCK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o007
            or (info.st_gid in (10001,10002,10003) and info.st_mode&0o070)
            or not stat.S_ISREG(binary.st_mode) or binary.st_uid!=0 or binary.st_mode&0o022):
        raise ContractError('Docker authority must be private to the trusted root broker')


class LocalDocker:
    def __init__(self,*,run=subprocess.run,popen=subprocess.Popen):
        self._run,self._popen=run,popen
        if run is subprocess.run and popen is subprocess.Popen:validate_docker_authority()

    def argv(self,args):return ['/usr/bin/docker','--host','unix://'+DOCKER_SOCKET,*args]

    def run(self,args,*,data=None,timeout=30,missing_ok=False):
        result=self._run(self.argv(args),input=data,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            timeout=timeout,env=dict(PATH='/usr/bin:/bin',LANG='C.UTF-8'),close_fds=True)
        if result.returncode:
            if missing_ok and result.returncode==1:return None
            raise ContractError('local Docker operation failed')
        if len(result.stdout)>2*1024**2:raise ContractError('Docker response exceeds limit')
        return result.stdout

    def inspect(self,reference,*,missing_ok=False):
        raw=self.run(['inspect','--type','container',reference],missing_ok=missing_ok)
        if raw is None:return None
        value=json.loads(raw)
        if type(value) is not list or len(value)!=1:raise ContractError('one execution container required')
        return value[0]

    def acquire(self,target,alias,deployment_id):
        data=self.inspect(target['container_id'] or target['name'],missing_ok=True)
        created=target['container_id'] is None
        if data is None:
            if not created:raise ContractError('registered execution container is missing')
            images=json.loads(self.run(['image','inspect',target['create_image']]))
            if type(images) is not list or len(images)!=1 or images[0].get('Id')!=target['create_image']:
                raise ContractError('prepared immutable execution image is not installed')
            budget=target['resource_limits']
            args=['create','--pull=never','--name',target['name'],'--label','c3.execution-deployment='+deployment_id,
                '--label','c3.execution-alias='+alias,'--user','0:0','--network','bridge','--ipc','private',
                '--cgroupns','private','--runtime','runc','--cap-drop','ALL','--cap-add','SETUID',
                '--cap-add','SETGID','--cap-add','CHOWN','--cap-add','KILL','--cap-add','DAC_OVERRIDE',
                '--security-opt','no-new-privileges:true','--cpus',str(target['cpu_cores']),
                '--memory',str(budget['memory_bytes']),'--memory-swap',str(budget['memory_bytes']),
                '--pids-limit',str(budget['max_processes']),'--shm-size','64m','--restart','no']
            if target['gpu_uuids']:args.extend(['--gpus','"device='+','.join(target['gpu_uuids'])+'"'])
            # Fixed idle PID1; the trusted per-job supervisor owns only its job's
            # descendants. Containers remain available for later approved jobs.
            args.extend(['--entrypoint',target['python'],target['create_image'],'-I','-B','-c',
                         'import time\nwhile True: time.sleep(3600)'])
            self.run(args);data=self.inspect(target['name'])
        ident=validate_container(data,target,created=created,deployment_id=deployment_id,alias=alias)
        if not data['State']['Running']:
            self.run(['start',ident]);data=self.inspect(ident)
            validate_container(data,target,created=created,deployment_id=deployment_id,alias=alias)
        return ident

    def install(self,ident,python,runtime_sha,*,source_root=Path('/opt/service'),
                input_root=Path('/opt/goal-inputs'),manifest_path=Path('/opt/c3-runtime/goal-inputs.json')):
        """Install public, versioned supervisor code only; no auth in the archive."""
        manifest_raw=manifest_path.read_bytes()
        destination=RUNTIME_ROOT+'/'+runtime_sha+'-'+hashlib.sha256(manifest_raw).hexdigest()[:16]
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w') as archive:
            paths=sorted((source_root/'research_harness').glob('*.py'))
            paths.extend([source_root/'execution_worker.py',source_root/'worker_pr.py'])
            for path in paths:
                info=path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
                    raise ContractError('immutable execution bootstrap required')
                raw=path.read_bytes();entry=tarfile.TarInfo(str(path.relative_to(source_root)))
                entry.uid=entry.gid=0;entry.mode=0o444;entry.size=len(raw)
                archive.addfile(entry,io.BytesIO(raw))
            manifest=json.loads(manifest_raw)
            for name,raw in [('goal-inputs.json',manifest_raw),
                    *[(str(Path('goal-inputs')/name),(input_root/name).read_bytes()) for name in manifest['files']]]:
                entry=tarfile.TarInfo(name);entry.uid=entry.gid=0;entry.mode=0o444;entry.size=len(raw)
                archive.addfile(entry,io.BytesIO(raw))
        payload=buffer.getvalue();digest=hashlib.sha256(payload).hexdigest()
        code=('import pathlib,stat,sys\n'
              'p=pathlib.Path(sys.argv[1]); root=p.parent\n'
              'for x in reversed([root,*root.parents]):\n'
              ' if not x.exists(): x.mkdir(mode=0o755)\n'
              ' s=x.lstat()\n'
              ' if not stat.S_ISDIR(s.st_mode) or x.is_symlink() or s.st_uid!=0 or s.st_mode&0o022: raise SystemExit(1)\n'
              'if p.exists():\n'
              ' marker=p/".installed"\n'
              ' s=p.lstat(); m=marker.lstat()\n'
              ' if p.is_symlink() or s.st_uid!=0 or s.st_mode&0o022 or not stat.S_ISREG(m.st_mode) or m.st_uid!=0 or m.st_mode&0o022 or m.st_nlink!=1 or marker.read_text()!=sys.argv[2]: raise SystemExit(1)\n'
              ' print("READY")\n'
              'else:\n'
              ' p.with_name(p.name+".install").mkdir(mode=0o700)\n'
              ' print("INSTALL")\n')
        result=self.run(['exec','--user','0:0',ident,python,'-I','-S','-B','-c',code,destination,digest])
        if result.strip()==b'READY':return destination
        if result.strip()!=b'INSTALL':raise ContractError('execution bootstrap preparation failed')
        pending=destination+'.install'
        self.run(['cp','-',ident+':'+pending+'/'],data=payload)
        finish=('import os,pathlib,sys\n'
                'p=pathlib.Path(sys.argv[1]); (p/".installed").write_text(sys.argv[2]); '
                '(p/".installed").chmod(0o444); p.chmod(0o755); os.rename(p,sys.argv[3])\n')
        self.run(['exec','--user','0:0',ident,python,'-I','-S','-B','-c',finish,pending,digest,destination])
        return destination

    def worker(self,ident,python,runtime):
        return self._popen(self.argv(['exec','-i','--user','0:0','--workdir','/',ident,
            python,'-I','-S','-B',runtime+'/execution_worker.py']),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,close_fds=True,bufsize=0,start_new_session=True,
            env=dict(PATH='/usr/bin:/bin',LANG='C.UTF-8'))
