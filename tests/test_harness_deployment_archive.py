import io
import tarfile
import unittest
from research_harness.deployment_archive import build_archive,verify_archive
from research_harness.schema import ContractError

class DeploymentArchiveTests(unittest.TestCase):
    def test_archive_uses_explicit_root_ownership_not_host(self):
        raw=build_archive(b'{"fixture":true}')
        verify_archive(raw,b'{"fixture":true}')
        with tarfile.open(fileobj=io.BytesIO(raw)) as t:
            i=t.getmembers()[0]
            self.assertEqual((i.uid,i.gid,i.mode),(0,0,0o644))
    def altered(self,**fields):
        out=io.BytesIO()
        with tarfile.open(fileobj=out,mode='w') as t:
            i=tarfile.TarInfo('deployment.json');i.uid=i.gid=0;i.mode=0o644;i.size=2
            for k,v in fields.items():setattr(i,k,v)
            t.addfile(i,io.BytesIO(b'{}') if i.isfile() else None)
        return out.getvalue()
    def test_root_owned_private_archive_is_rejected(self):
        with self.assertRaises(ContractError):verify_archive(self.altered(mode=0o600),b'{}')
    def test_host_owner_and_unsafe_modes_and_links_are_rejected(self):
        for fields in ({'uid':1000},{'gid':1000},{'mode':0o666},{'mode':0o600},{'name':'../deployment.json'},
                       {'type':tarfile.SYMTYPE,'linkname':'/tmp/elsewhere','size':0}):
            with self.subTest(fields=fields),self.assertRaises(ContractError):verify_archive(self.altered(**fields),b'{}')
    def test_payload_mismatch_or_invalid_archive_rejected(self):
        for raw in (b'bad',build_archive(b'{"other":1}')):
            with self.assertRaises(ContractError):verify_archive(raw,b'{}')
