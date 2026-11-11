import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_harness.goal_artifacts import collect_goal_changes, snapshot_goal_workspace
from research_harness.schema import ContractError

SHA = 'a' * 40


class GoalArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'input.txt').write_text('approved input')
        (self.root / 'out').mkdir()

    def snapshot(self, **kwargs):
        args = dict(source_sha=SHA, write_scope=['out'], worker_stopped=True)
        args.update(kwargs)
        return snapshot_goal_workspace(self.root, **args)

    def collect(self, baseline, **kwargs):
        args = dict(source_sha=SHA, write_scope=['out'], worker_stopped=True)
        args.update(kwargs)
        return collect_goal_changes(self.root, baseline, **args)

    def test_changes_are_frozen_unapproved_and_bound(self):
        baseline = self.snapshot()
        (self.root / 'out/result.txt').write_text('new text')
        candidate = self.collect(baseline)
        self.assertFalse(candidate.publish_authorized)
        self.assertTrue(candidate.requires_content_review)
        self.assertEqual(candidate.baseline_digest, baseline.snapshot_digest)
        self.assertEqual(candidate.changes[0].after.content, b'new text')
        digest = candidate.candidate_digest
        (self.root / 'out/result.txt').write_text('changed after collection')
        self.assertEqual(candidate.candidate_digest, digest)
        self.assertNotEqual(self.collect(baseline).candidate_digest, digest)

    def test_noop(self):
        baseline = self.snapshot()
        self.assertEqual(self.collect(baseline).changes, ())

    def test_deletion_is_bound(self):
        file = self.root / 'out/result.txt'
        file.write_text('old')
        baseline = self.snapshot()
        file.unlink()
        change = self.collect(baseline).changes[0]
        self.assertEqual(change.before.content, b'old')
        self.assertIsNone(change.after)

    def test_reject_source_scope_or_worker_mismatch(self):
        baseline = self.snapshot()
        for args in ({'source_sha': 'b' * 40}, {'write_scope': ['input.txt']},
                     {'worker_stopped': False}, {'worker_stopped': 1}):
            with self.subTest(args=args), self.assertRaises(ContractError):
                self.collect(baseline, **args)

    def test_reject_out_of_scope_add_edit_delete_mode(self):
        for operation in ('add', 'edit', 'delete', 'mode'):
            with self.subTest(operation=operation):
                baseline = self.snapshot()
                p = self.root / 'input.txt'
                if operation == 'add':
                    (self.root / 'outsider.txt').write_text('no')
                elif operation == 'edit':
                    p.write_text('no')
                elif operation == 'delete':
                    p.unlink()
                else:
                    p.chmod(0o700)
                with self.assertRaises(ContractError):
                    self.collect(baseline)
                (self.root / 'outsider.txt').unlink(missing_ok=True)
                p.write_text('approved input')
                p.chmod(0o644)

    def test_reject_links_and_special_files(self):
        p = self.root / 'out/bad'
        for kind in ('symlink', 'directory_symlink', 'hardlink', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    p.symlink_to(self.root / 'input.txt')
                elif kind == 'directory_symlink':
                    p.symlink_to(self.root / 'out', target_is_directory=True)
                elif kind == 'hardlink':
                    os.link(self.root / 'input.txt', p)
                else:
                    os.mkfifo(p)
                with self.assertRaises(ContractError):
                    self.snapshot()
                p.unlink()

    def test_reject_root_symlink(self):
        link = self.root / 'link'
        link.symlink_to(self.root / 'out', target_is_directory=True)
        with self.assertRaises(ContractError):
            snapshot_goal_workspace(link, source_sha=SHA, write_scope=['out'], worker_stopped=True)

    def test_reject_binary_and_oversize(self):
        p = self.root / 'out/bad'
        for data in (b'\xff', b'abc\x00', b'x' * (1024 * 1024 + 1)):
            p.write_bytes(data)
            with self.assertRaises(ContractError):
                self.snapshot()
        p.write_text('12345')
        with self.assertRaises(ContractError):
            self.snapshot(max_total_bytes=15)

    def test_reject_changed_second_scan(self):
        from research_harness import goal_artifacts
        original = goal_artifacts._scan
        count = 0
        def scan(*args):
            nonlocal count
            result = original(*args)
            count += 1
            if count == 1:
                (self.root / 'out/changed').write_text('race')
            return result
        with patch.object(goal_artifacts, '_scan', side_effect=scan), self.assertRaises(ContractError):
            self.snapshot()

    def test_reject_replacement_during_open(self):
        from research_harness import goal_artifacts
        original = os.open
        changed = False
        def raced_open(path, *args, **kwargs):
            nonlocal changed
            if path == 'input.txt' and not changed:
                changed = True
                (self.root / 'input.txt').unlink()
                (self.root / 'input.txt').write_text('replacement')
            return original(path, *args, **kwargs)
        with patch.object(goal_artifacts.os, 'open', side_effect=raced_open), self.assertRaises(ContractError):
            self.snapshot()

    def test_input_binding_roundtrip_and_approved_larger_limit(self):
        from research_harness.goal_artifacts import GoalChangeCandidate
        baseline = self.snapshot(inputs=['input.txt'], max_file_bytes=2 * 1024 * 1024,
                                 max_total_bytes=2 * 1024 * 1024)
        (self.root / 'out/larger.txt').write_bytes(b'x' * (1024 * 1024 + 1))
        candidate = self.collect(baseline, inputs=['input.txt'])
        restored = GoalChangeCandidate.from_dict(candidate.to_dict())
        self.assertEqual(restored, candidate)
        self.assertEqual(restored.candidate_digest, candidate.candidate_digest)
        with self.assertRaises(ContractError):
            self.collect(baseline, inputs=[])

    def test_reject_invalid_scope_and_byte_limits(self):
        for scope in ([], ['../out'], ['out', 'out'], ['out/*'], ['.git']):
            with self.subTest(scope=scope), self.assertRaises(ContractError):
                self.snapshot(write_scope=scope)
        for limit in (0, True, 16 * 1024 * 1024 + 1):
            with self.subTest(limit=limit), self.assertRaises(ContractError):
                self.snapshot(max_total_bytes=limit)


if __name__ == '__main__':
    unittest.main()
