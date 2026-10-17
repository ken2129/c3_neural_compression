"""Create and verify a Docker-copy archive without inheriting host ownership."""
import io
import tarfile
from .schema import ContractError


def build_archive(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 1024*1024:
        raise ContractError('invalid deployment archive payload')
    target=io.BytesIO()
    with tarfile.open(fileobj=target,mode='w') as archive:
        info=tarfile.TarInfo('deployment.json')
        info.uid=info.gid=10001;info.uname=info.gname='';info.mode=0o600
        info.size=len(raw);info.mtime=0
        archive.addfile(info,io.BytesIO(raw))
    return target.getvalue()


def verify_archive(raw,expected):
    if type(raw) is not bytes or len(raw)>2*1024*1024:
        raise ContractError('invalid deployment copy verification')
    try:
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
            members=archive.getmembers()
            if len(members)!=1:raise ValueError()
            entry=members[0]
            if (entry.name!='deployment.json' or not entry.isfile()
                or entry.uid!=10001 or entry.gid!=10001
                or entry.mode!=0o600 or entry.size!=len(expected)):
                raise ValueError()
            if archive.extractfile(entry).read()!=expected:raise ValueError()
    except (tarfile.TarError,ValueError,AttributeError,OSError):
        raise ContractError('deployment copy ownership or contents differ') from None
