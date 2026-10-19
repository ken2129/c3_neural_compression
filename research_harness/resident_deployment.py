"""Validate host-collected dedicated-container facts before accepting a restart.

This is not an in-container Docker identity oracle. The installation operator
must supply authentic Docker inspect results and a complete list of volume
consumers, then protect the resulting manifest from the worker. No Docker
socket is exposed to the service. Host administrators remain trusted.
"""
import posixpath
import re
from .schema import ContractError


def validate_resident_deployment(inspect, consumers, *, runtime_source_sha, input_source_sha,
                             volume_name, volume_inspect, resource_limits, deployment_id):
    return _validate(inspect,consumers,runtime_source_sha=runtime_source_sha,
        input_source_sha=input_source_sha,volume_name=volume_name,volume_inspect=volume_inspect,
        resource_limits=resource_limits,deployment_id=deployment_id,
        purpose='dedicated-resident-controller',script='/opt/service/resident_launcher.py',restart='unless-stopped')


def validate_diagnostic_deployment(inspect,consumers,**kwargs):
    """Only the separately installed fixed model-free diagnostic entrypoint."""
    if type(inspect) is not dict or inspect.get('AppArmorProfile')!='docker-default':
        raise ContractError('diagnostic requires docker-default AppArmor')
    return _validate(inspect,consumers,**kwargs,purpose='dedicated-resident-diagnostic',
        script='/opt/service/resident_diagnostic.py',restart='no')


def _validate(inspect,consumers,*,runtime_source_sha,input_source_sha,volume_name,
              volume_inspect,resource_limits,deployment_id,purpose,script,restart):
    """Validate a new goal-only installation; never normalize canary evidence."""
    source_sha = runtime_source_sha
    if (type(input_source_sha) is not str or re.fullmatch("[0-9a-f]{40}", input_source_sha) is None
            or type(resource_limits) is not dict
            or type(deployment_id) is not str or re.fullmatch("[0-9a-f]{32}", deployment_id) is None):
        raise ContractError("invalid goal deployment configuration")
    try:
        ident=inspect['Id'];config=inspect['Config'];host=inspect['HostConfig']
        image_id=inspect['Image']
        if type(image_id) is not str or re.fullmatch('sha256:[0-9a-f]{64}',image_id) is None:raise ValueError()
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{64}',ident):raise ValueError()
        if type(source_sha) is not str or not re.fullmatch('[0-9a-f]{40}',source_sha):raise ValueError()
        if type(volume_name) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',volume_name):raise ValueError()
        if config['Labels'].get('c3.purpose')!=purpose:raise ValueError()
        if config['Labels'].get('c3.source-sha')!=source_sha:raise ValueError()
        if config['Labels'].get('c3.input-source-sha')!=input_source_sha:raise ValueError()
        if config['Labels'].get('c3.deployment-id')!=deployment_id:raise ValueError()
        if config['Hostname']!=ident[:12] or config['User'] not in ('','0','0:0'):raise ValueError()
        if host['Privileged'] is not False or host['ReadonlyRootfs'] is not True:raise ValueError()
        if host['PidMode']!='' or host['IpcMode']!='private':raise ValueError()
        if host['CgroupnsMode']!='private':raise ValueError()
        if type(host['CapAdd']) is not list or type(host['CapDrop']) is not list:raise ValueError()
        # Docker versions differ in whether capability names include CAP_.
        if any(type(cap) is not str for key in ('CapAdd','CapDrop') for cap in host[key]):raise ValueError()
        capabilities=lambda key: {cap.removeprefix('CAP_') for cap in host[key]}
        if any(len(capabilities(key))!=len(host[key]) for key in ('CapAdd','CapDrop')):raise ValueError()
        if capabilities('CapAdd')!={'SETUID','SETGID'} or capabilities('CapDrop')!={'ALL'}:raise ValueError()
        if host['SecurityOpt']!=['no-new-privileges:true']:raise ValueError()
        if host['NetworkMode']!='bridge':raise ValueError()
        if config['Entrypoint']!=['python','-I','-B',script] or config['Cmd'] not in (None,[]):raise ValueError()
        if host['Init'] is not False:raise ValueError()
        if host['RestartPolicy']['Name']!=restart:raise ValueError()
        if host['Runtime']!='runc' or host.get('DeviceCgroupRules') not in (None,[]):raise ValueError()
        work_bytes=resource_limits['max_work_bytes']
        if type(work_bytes) is not int or not 1024**2<=work_bytes<=256*1024**2:raise ValueError()
        if host['Tmpfs']!={'/work':f'rw,noexec,nosuid,nodev,size={work_bytes},mode=0755,uid=0,gid=0','/control-ipc':'rw,noexec,nosuid,nodev,size=1048576,mode=0711,uid=10001,gid=10001'}:raise ValueError()
        if host['ShmSize']!=64*1024**2:raise ValueError()
        if type(host['MemorySwap']) is not int or host['MemorySwap']!=host['Memory']:raise ValueError()
        runner_volume = volume_name.removesuffix('_goal-control') + '_runner-state'
        if volume_name == runner_volume or not volume_name.endswith('_goal-control'):raise ValueError()
        expected_binds={volume_name+':/control',runner_volume+':/runner'}
        binds=host.get('Binds')
        if binds not in (None,[]) and set(binds) not in (expected_binds,{v+':rw' for v in expected_binds}):raise ValueError()
        if any(host.get(key) not in (None,[]) for key in ('Devices','DeviceRequests')):raise ValueError()
        if any(type(host[key]) is not int for key in ('Memory','PidsLimit','NanoCpus')):raise ValueError()
        if not 64*1024**2<=host['Memory']<=1024**3 or not 2<=host['PidsLimit']<=64 or host['NanoCpus']!=10**9:raise ValueError()
        if host['Memory']!=resource_limits['memory_bytes'] or host['PidsLimit']!=resource_limits['max_processes']:raise ValueError()
        mounts=inspect['Mounts']
        if type(mounts) is not list or len(mounts)!=2:raise ValueError()
        if type(volume_inspect) is not dict or set(volume_inspect)!={'control','runner'}:raise ValueError()
        if {m['Destination'] for m in mounts}!={'/control','/runner'}:raise ValueError()
        for destination,name,key in [('/control',volume_name,'control'),('/runner',runner_volume,'runner')]:
            mount=next(m for m in mounts if m['Destination']==destination)
            if mount['Type']!='volume' or mount['Name']!=name or mount['RW'] is not True:raise ValueError()
            info=volume_inspect[key]
            if (info['Name']!=name or info['Driver']!='local' or info['Scope']!='local'
                or info['Options'] not in (None,{}) or type(info.get('Labels')) is not dict
                or info['Labels'].get('c3.deployment-id')!=deployment_id):raise ValueError()
            source=mount['Source'];mountpoint=info['Mountpoint']
            if (type(source) is not str or type(mountpoint) is not str
                or not source.startswith('/') or source.startswith('//') or '\x00' in source
                or posixpath.normpath(source)!=source or source!=mountpoint):raise ValueError()
        # Both private volumes must be exclusive. Checking only /control would
        # permit another runner to mutate this runner's registration/package.
        if (type(consumers) is not dict or set(consumers)!={'control','runner'}
            or any(consumers[key] != [ident] for key in ('control','runner'))):raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError):
        raise ContractError('resident deployment facts are incomplete or do not match') from None
    return dict(schema_version=1,container_id=ident,source_sha=input_source_sha,
                runtime_source_sha=runtime_source_sha,input_source_sha=input_source_sha,
                image_id=image_id,volume_name=volume_name,deployment_id=deployment_id,
                pid_mode='private',restart_policy=restart,exclusive_volume=True)
