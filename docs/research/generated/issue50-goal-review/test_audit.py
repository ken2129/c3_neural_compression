"""Public aggregate fixtures only; mutation tests do not modify source files."""
import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import audit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / audit.INPUTS[0]).read_text())
        self.rows = list(csv.DictReader(io.StringIO((ROOT / audit.INPUTS[1]).read_text())))

    def result(self):
        return audit.audit(self.data, self.rows)

    def rejected(self):
        with self.assertRaises(ValueError):
            self.result()

    def test_valid_counts_and_groups(self):
        s = self.result()
        self.assertEqual(s['audit']['compressed_points'], 24)
        self.assertEqual(s['audit']['csv_rows'], 25)
        self.assertEqual(len(s['groups']), 6)
        self.assertTrue(all(g['count'] == 4 for g in s['groups'].values()))
        self.assertEqual(s['audit']['primary_points'], 16)
        self.assertEqual(s['audit']['supplementary_external_uint8_points'], 8)
        self.assertEqual(s['audit']['all_uint8_reused_C3_points'], 8)

    def test_overlap_and_precision_deltas(self):
        s = self.result()
        self.assertEqual(s['four_way_rate_overlap'], [0.11394990474969535, 0.27705573918739584])
        self.assertEqual(len(s['pairwise_rate_overlap']), 6)
        deltas = [d['uint8_minus_float32_AP_pp'] for d in s['precision_AP_deltas']]
        self.assertEqual(len(deltas), 8)
        self.assertAlmostEqual(deltas[0], 0.8149239607648302)
        self.assertAlmostEqual(deltas[-1], 0.7756727729157586)
        self.assertEqual(sum(d > 0 for d in deltas), 4)

    def test_jpeg_null_and_csv_only_sensitivity(self):
        s = self.result()['JPEG']
        self.assertIsNone(s['bpp'])
        self.assertEqual(s['AP_percent'], 47.432959401770944)
        self.assertEqual(s['CSV_only_delete_one_AP_range'], [44.11318318086129, 51.874101429124806])
        self.assertFalse(s['CSV_only_range_cross_checked_with_JSON'])

    def test_order_independence_and_determinism(self):
        before = json.dumps(self.result(), sort_keys=True, allow_nan=False)
        self.rows.reverse()
        self.data['points'].reverse()
        self.assertEqual(before, json.dumps(self.result(), sort_keys=True, allow_nan=False))

    def test_no_input_mutation(self):
        before = copy.deepcopy((self.data, self.rows))
        self.result()
        self.assertEqual(before, (self.data, self.rows))

    def test_missing_json_point(self):
        self.data['points'].pop()
        self.rejected()

    def test_missing_csv_point(self):
        self.rows.pop(0)
        self.rejected()

    def test_duplicate_json(self):
        self.data['points'][1] = copy.deepcopy(self.data['points'][0])
        self.rejected()

    def test_duplicate_csv(self):
        self.rows[1] = dict(self.rows[0])
        self.rejected()

    def test_missing_metric(self):
        del self.data['points'][0]['box_ap50']
        self.rejected()

    def test_metric_mismatch(self):
        self.rows[0]['box_ap_large'] = '1'
        self.rejected()

    def test_nonfinite(self):
        self.rows[0]['bpp'] = 'nan'
        self.rejected()

    def test_negative_rate(self):
        self.rows[0]['bpp'] = '-1'
        self.data['points'][0]['bpp'] = -1
        self.rejected()

    def test_unexpected_precision(self):
        self.rows[0]['precision'] = 'float16'
        self.rejected()

    def test_missing_jpeg(self):
        self.rows.pop()
        self.rejected()

    def test_jpeg_zero_bpp(self):
        self.rows[-1]['bpp'] = '0'
        self.rejected()

    def test_jpeg_ap_mismatch(self):
        self.rows[-1]['box_ap'] = '0'
        self.rejected()

    def test_reversed_sensitivity(self):
        self.rows[-1]['delete_one_AP_min'] = '99'
        self.rejected()

    def test_overlap_mismatch(self):
        self.data['four_way_rate_overlap'][0] = 0
        self.rejected()

    def test_pairwise_overlap_mismatch(self):
        self.data['pairwise_rate_range_overlap']['c3 / c3_prime'][0] = 0
        self.rejected()

    def test_image_count_mismatch(self):
        self.data['image_count'] = 21
        self.rejected()

    def test_disjoint_overlap(self):
        self.assertIsNone(audit.overlap([[0, 1], [2, 3]]))

    def cli_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in audit.INPUTS + [str(audit.SCOPE / 'test-results.json')]:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        return root

    def run_cli(self, root):
        return subprocess.run(
            [sys.executable, '-B', str(HERE / 'audit.py'), '--root', str(root),
             '--output', str(audit.SCOPE / 'cli-test')],
            capture_output=True, text=True, timeout=10)

    def assert_cli_rejected(self, root, message):
        out = root / audit.SCOPE / 'cli-test'
        result = self.run_cli(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertNotIn('PASS:', result.stdout)
        self.assertFalse(out.exists())
        # A failed rerun must also leave previous report files untouched.
        out.mkdir()
        for name in ['summary.json', 'report.md']:
            (out / name).write_bytes(b'existing output\n')
        result = self.run_cli(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertNotIn('PASS:', result.stdout)
        for name in ['summary.json', 'report.md']:
            self.assertEqual((out / name).read_bytes(), b'existing output\n')

    def test_approved_snapshot_cli_and_determinism(self):
        root = self.cli_fixture()
        result = self.run_cli(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS:', result.stdout)
        out = root / audit.SCOPE / 'cli-test'
        before = {name: (out / name).read_bytes() for name in ['summary.json', 'report.md']}
        summary = json.loads(before['summary.json'])
        self.assertEqual(summary['inputs'], [
            {'path': path, 'sha256': audit.EXPECTED_INPUT_SHA256[path],
             'source_sha': audit.SOURCE_SHA} for path in audit.INPUTS])
        self.assertIn(b'all three public inputs match the SHA256 digests', before['report.md'])
        result = self.run_cli(root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {name: (out / name).read_bytes() for name in before})

    def test_each_input_digest_is_checked(self):
        for path in audit.INPUTS:
            with self.subTest(path=path):
                root = self.cli_fixture()
                target = root / path
                target.write_bytes(target.read_bytes() + b'\n')
                with self.assertRaisesRegex(ValueError, 'input snapshot mismatch') as error:
                    audit.load_inputs(root)
                self.assertIn(path, str(error.exception))

    def test_cli_rejects_coherently_modified_json_csv(self):
        root = self.cli_fixture()
        point = self.data['points'][0]
        point['box_ap'] += 0.125
        row = next(row for row in self.rows if audit.key(row) == audit.key(point))
        row['box_ap'] = str(point['box_ap'])
        # The regression fixture still passes the old aggregate-only checks.
        self.assertEqual(self.result()['audit']['status'], 'pass')
        (root / audit.INPUTS[0]).write_text(json.dumps(self.data), encoding='utf-8')
        with (root / audit.INPUTS[1]).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
            writer.writeheader()
            writer.writerows(self.rows)
        self.assert_cli_rejected(root, 'input snapshot mismatch: ' + audit.INPUTS[0])

    def test_cli_rejects_modified_markdown(self):
        root = self.cli_fixture()
        target = root / audit.INPUTS[2]
        target.write_bytes(target.read_bytes() + b'\nAltered historical context.\n')
        self.assert_cli_rejected(root, 'input snapshot mismatch: ' + audit.INPUTS[2])

    def test_cli_rejects_missing_input(self):
        root = self.cli_fixture()
        (root / audit.INPUTS[2]).unlink()
        self.assert_cli_rejected(root, audit.INPUTS[2])


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes.append({'test': test.id(), 'outcome': 'pass'})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes.append({'test': test.id(), 'outcome': 'failure'})

    def addError(self, test, err):
        super().addError(test, err)
        self.outcomes.append({'test': test.id(), 'outcome': 'error'})


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AuditTests)
    stream = io.StringIO()
    r = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=RecordedResult).run(suite)
    # Exclude nondeterministic duration from the persisted evidence.
    evidence = {'tests_run': r.testsRun, 'failures': len(r.failures), 'errors': len(r.errors),
                'skipped': len(r.skipped), 'outcomes': sorted(r.outcomes, key=lambda x: x['test'])}
    (HERE / 'test-results.json').write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    print(stream.getvalue(), end='')
    raise SystemExit(0 if r.wasSuccessful() else 1)
