import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from research_harness.persistent_fixture import initialize, open_existing, run_persistent
from research_harness.intake_flow_fixture import dummy_result
from research_harness.schema import ContractError


class PersistentFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.root.chmod(0o700)
        (self.root/'.layout').write_text('offline-fixture-v1\n')
        initialize(self.root)
        self.calls=[]

    def exchange(self,job):
        self.calls.append(job);return dummy_result(job)

    def test_reopen_persists_result_without_dispatch(self):
        first=run_persistent(self.root,self.exchange)
        second=run_persistent(self.root,self.exchange)
        self.assertTrue(first['dispatched']);self.assertFalse(second['dispatched'])
        self.assertTrue(second['duplicate']);self.assertTrue(second['result_saved'])
        self.assertEqual(len(self.calls),1)

    def test_initialization_never_resets_existing_volume(self):
        with self.assertRaises(ContractError):initialize(self.root)

    def test_missing_database_not_recreated(self):
        (self.root/'state.sqlite').unlink()
        with self.assertRaises(FileNotFoundError):run_persistent(self.root,self.exchange)
        self.assertFalse((self.root/'state.sqlite').exists());self.assertEqual(self.calls,[])

    def test_corrupt_database_blocks(self):
        (self.root/'state.sqlite').write_bytes(b'corrupt')
        with self.assertRaises(sqlite3.DatabaseError):run_persistent(self.root,self.exchange)
        self.assertEqual(self.calls,[])

    def test_wrong_permissions_blocks(self):
        (self.root/'state.sqlite').chmod(0o644)
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)

    def test_symlink_blocks(self):
        db=self.root/'state.sqlite';db.rename(self.root/'other.sqlite');db.symlink_to('other.sqlite')
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)

    def test_unknown_schema_blocks(self):
        db=sqlite3.connect(self.root/'state.sqlite');db.execute('CREATE TABLE unexpected(x)');db.commit();db.close()
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)

    def test_volume_identity_mismatch_blocks(self):
        db=sqlite3.connect(self.root/'state.sqlite');db.execute("UPDATE volume_identity SET nonce='other'");db.commit();db.close()
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)

    def test_deleted_run_does_not_redispatch(self):
        run_persistent(self.root,self.exchange)
        db=sqlite3.connect(self.root/'state.sqlite');db.execute('DELETE FROM offline_dummy_runs');db.commit();db.close()
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)
        self.assertEqual(len(self.calls),1)

    def test_corrupt_result_does_not_pass_as_completed(self):
        run_persistent(self.root,self.exchange)
        db=sqlite3.connect(self.root/'state.sqlite');db.execute("UPDATE offline_dummy_runs SET result='{}'");db.commit();db.close()
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)
        self.assertEqual(len(self.calls),1)

    def test_failed_claim_cannot_retry(self):
        def fail(job):self.calls.append(job);raise RuntimeError('uncertain')
        with self.assertRaises(RuntimeError):run_persistent(self.root,fail)
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)
        self.assertEqual(len(self.calls),1)

    def test_missing_dispatch_witness_blocks_completed(self):
        run_persistent(self.root,self.exchange);(self.root/'dispatch.json').unlink()
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)
        self.assertEqual(len(self.calls),1)

    def test_same_columns_without_constraints_rejected(self):
        db=sqlite3.connect(self.root/'state.sqlite')
        db.execute('DROP TABLE offline_dummy_runs')
        db.execute('CREATE TABLE offline_dummy_runs (issue INTEGER,digest TEXT,phase TEXT,result TEXT)')
        db.commit();db.close()
        with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)

    def test_validated_connection_not_reopened(self):
        from unittest.mock import patch
        with patch('research_harness.persistent_fixture.Intake.__init__',side_effect=AssertionError('must not reopen')):
            store=open_existing(self.root);store.close()

    def test_missing_seeded_rows_are_not_repaired(self):
        for table in ('intake','owner_receipts'):
            with self.subTest(table=table):
                db=sqlite3.connect(self.root/'state.sqlite')
                original=db.execute('SELECT * FROM '+table).fetchall()
                db.execute('DELETE FROM '+table);db.commit();db.close()
                with self.assertRaises(ContractError):run_persistent(self.root,self.exchange)
                db=sqlite3.connect(self.root/'state.sqlite')
                self.assertEqual(db.execute('SELECT * FROM '+table).fetchall(),[])
                db.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in original[0])+')',original)
                db.commit();db.close()
