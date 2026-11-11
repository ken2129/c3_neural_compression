"""Validate host-collected dedicated-container facts before accepting a restart.

This is not an in-container Docker identity oracle. The installation operator
must supply authentic Docker inspect results and a complete list of volume
consumers, then protect the resulting manifest from the worker. No Docker
socket is exposed to the service. Host administrators remain trusted.
"""
import posixpath
import re
from .schema import ContractError


def validate_dedicated_deployment(inspect,consumers,*,source_sha,volume_name,volume_inspect=None):
    try:
        ident=inspect['Id'];config=inspect['Config'];host=inspect['HostConfig']
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{64}',ident):raise ValueError()
        if type(source_sha) is not str or not re.fullmatch('[0-9a-f]{40}',source_sha):raise ValueError()
        if type(volume_name) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',volume_name):raise ValueError()
        if config['Labels'].get('c3.purpose')!='dedicated-research-controller':raise ValueError()
        if config['Labels'].get('c3.source-sha')!=source_sha:raise ValueError()
        if config['Hostname']!=ident[:12] or config['User'] not in ('','0','0:0'):raise ValueError()
        if host['Privileged'] is not False or host['ReadonlyRootfs'] is not True:raise ValueError()
        if host['PidMode']!='' or host['IpcMode'] not in ('private','none'):raise ValueError()
        if host['CgroupnsMode']!='private':raise ValueError()
        if type(host['CapAdd']) is not list or type(host['CapDrop']) is not list:raise ValueError()
        # Docker versions differ in whether capability names include CAP_.
        if any(type(cap) is not str for key in ('CapAdd','CapDrop') for cap in host[key]):raise ValueError()
        capabilities=lambda key: {cap.removeprefix('CAP_') for cap in host[key]}
        if any(len(capabilities(key))!=len(host[key]) for key in ('CapAdd','CapDrop')):raise ValueError()
        if capabilities('CapAdd')!={'SETUID','SETGID'} or capabilities('CapDrop')!={'ALL'}:raise ValueError()
        if host['SecurityOpt']!=['no-new-privileges:true']:raise ValueError()
        if host['NetworkMode']!='bridge':raise ValueError()
        if config['Entrypoint']!=['python','-I','-B','/opt/service/launcher.py'] or config['Cmd'] not in (None,[]):raise ValueError()
        if host['Init'] is not False:raise ValueError()
        if host['RestartPolicy']['Name']!='unless-stopped':raise ValueError()
        if host['Runtime']!='runc' or host['DeviceCgroupRules'] not in (None,[]):raise ValueError()
        if host['Tmpfs']!={'/work':'rw,noexec,nosuid,nodev,size=256m,mode=0700,uid=10002,gid=10002'}:raise ValueError()
        if type(host['MemorySwap']) is not int or host['MemorySwap']!=host['Memory']:raise ValueError()
        # -v records a named volume in Binds as well as the resolved Mounts.
        if host['Binds'] not in (None,[],[volume_name+':/control'],[volume_name+':/control:rw']):raise ValueError()
        if any(host[key] not in (None,[]) for key in ('Devices','DeviceRequests')):raise ValueError()
        if any(type(host[key]) is not int for key in ('Memory','PidsLimit','NanoCpus')):raise ValueError()
        if not 0<host['Memory']<=1024**3 or not 0<host['PidsLimit']<=64 or not 0<host['NanoCpus']<=10**9:raise ValueError()
        mounts=inspect['Mounts']
        if type(mounts) is not list or len(mounts)!=1:raise ValueError()
        mount=mounts[0]
        if mount['Type']!='volume' or mount['Name']!=volume_name or mount['Destination']!='/control' or mount['RW'] is not True:raise ValueError()
        if (type(volume_inspect) is not dict or volume_inspect['Name']!=volume_name
            or volume_inspect['Driver']!='local' or volume_inspect['Scope']!='local'
            or volume_inspect['Options'] not in (None,{})):raise ValueError()
        source=mount['Source'];mountpoint=volume_inspect['Mountpoint']
        if (type(source) is not str or type(mountpoint) is not str
            or not source.startswith('/') or source.startswith('//') or '\x00' in source
            or posixpath.normpath(source)!=source or source!=mountpoint):raise ValueError()
        if type(consumers) is not list or len(consumers)!=1 or consumers[0]!=ident:raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError):
        raise ContractError('dedicated deployment facts are incomplete or do not match') from None
    return dict(schema_version=1,container_id=ident,source_sha=source_sha,volume_name=volume_name,
                pid_mode='private',restart_policy='unless-stopped',exclusive_volume=True)
