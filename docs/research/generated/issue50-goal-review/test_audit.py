"""Public aggregate fixtures only; mutation tests do not modify source files."""
import copy
import csv
import io
import json
from pathlib import Path
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
