"""Independent, offline acceptance oracle for the proposed Issue50 CPU goal.

Only already-public repository JSON/CSV are read. No model, credentials,
container, remote machine, GPU, GitHub write, or result publication is involved.
The source SHA of this working copy has NOT been verified: these tests establish
local fixture consistency, not approved-revision provenance or production success.

The normalized summary below is a test adapter, not a mandatory model response
schema, command, implementation language, filename, or prose template. A runner's
successful candidate must still pass content checks; it is not proof of research
success. Runner protocol replay (including edit/failure/fix/multiple commands) is
owned by test_harness_goal_runner.py, not duplicated here.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_JSON = ROOT / 'docs/research/assets/issue50_four_method.json'
PUBLIC_CSV = ROOT / 'docs/research/assets/issue50_four_method.csv'
D = Decimal
METRICS = ('box_ap', 'box_ap50', 'box_ap75', 'box_ap_small',
           'box_ap_medium', 'box_ap_large')
POINT_NUMBERS = ('bpp',) + METRICS + ('delete_one_AP_min', 'delete_one_AP_max')
GROUPS = {
    ('adapt_icmh', 'float32'), ('adapt_icmh', 'uint8'),
    ('transtic', 'float32'), ('transtic', 'uint8'),
    ('c3', 'uint8'), ('c3_prime', 'uint8'),
}
# Transcribed from the public point rows, independently of their precomputed
# rate_ranges/four_way_rate_overlap fields. Decimal preserves the source values.
EXPECTED_RANGES = {
    ('adapt_icmh', 'float32'): (
        '0.09387180804749973', '0.27705573918739584',
        '42.657516264253175', '48.03575454294868'),
    ('adapt_icmh', 'uint8'): (
        '0.09387180804749973', '0.27705573918739584',
        '43.472440225018005', '47.354949297733626'),
    ('transtic', 'float32'): (
        '0.11394990474969535', '0.33941894029419173',
        '41.47825055150135', '46.12199844211216'),
    ('transtic', 'uint8'): (
        '0.11394990474969535', '0.33941894029419173',
        '41.23298067925865', '46.540253311135416'),
    ('c3', 'uint8'): (
        '0.1051765044229496', '0.3169445500418172',
        '29.733242364695784', '41.971638025658436'),
    ('c3_prime', 'uint8'): (
        '0.09457139983328176', '0.2897194231980741',
        '36.164844702269846', '42.390498997230246'),
}


class ResearchAcceptanceError(ValueError):
    pass


def require(condition, reason):
    # This is an oracle, so Python -O must not remove rejection checks.
    if not condition:
        raise ResearchAcceptanceError(reason)


def read_public_inputs():
    data = json.loads(PUBLIC_JSON.read_text(), parse_float=D)
    rows = list(csv.DictReader(io.StringIO(PUBLIC_CSV.read_text())))
    return data, rows


def number(value):
    require(not isinstance(value, bool), 'boolean is not a metric')
    try:
        result = D(str(value))
    except Exception as exc:
        raise ResearchAcceptanceError('invalid metric') from exc
    require(result.is_finite(), 'nonfinite metric')
    return result


def validate_public_pair(data, rows):
    """Independently match point identities and shared values across formats."""
    require(data['image_count'] == 20, 'unexpected evaluation image count')
    points = data['points']
    require(len(points) == 24 and len(rows) == 25, 'missing or added points')
    json_points = {}
    for point in points:
        key = (point['method'], str(point['quality']), point['precision'])
        require(key not in json_points, 'duplicate JSON point')
        json_points[key] = point
    csv_points = {}
    baselines = []
    for row in rows:
        if row['method'] == 'JPEG_baseline':
            baselines.append(row)
            continue
        key = (row['method'], row['quality'], row['precision'])
        require(key not in csv_points, 'duplicate CSV point')
        csv_points[key] = row
    require(json_points.keys() == csv_points.keys(), 'point identity mismatch')
    for key, point in json_points.items():
        for metric in POINT_NUMBERS:
            require(number(point[metric]) == number(csv_points[key][metric]),
                    'JSON/CSV metric mismatch')
    require(len(baselines) == 1, 'exactly one separate JPEG baseline required')
    baseline = baselines[0]
    require(baseline['bpp'] == '' and baseline['quality'] == ''
            and baseline['precision'] == 'original_JPEG', 'JPEG is not a rate point')
    for metric in METRICS:
        require(number(data['JPEG_baseline'][metric]) == number(baseline[metric]),
                'JPEG metric mismatch')
    # JPEG delete-one values only exist in CSV; do not invent JSON equivalents.
    require(number(baseline['delete_one_AP_min']) <= number(baseline['box_ap'])
            <= number(baseline['delete_one_AP_max']), 'invalid JPEG sensitivity range')
    counts = Counter((p['method'], p['precision']) for p in points)
    require(set(counts) == GROUPS and set(counts.values()) == {4},
            'precision groups missing or conflated')
    return points


def independent_summary(data, rows):
    points = validate_public_pair(data, rows)
    groups = defaultdict(list)
    for point in points:
        groups[(point['method'], point['precision'])].append(point)
    result_groups = {}
    primary_ranges = []
    primary_count = auxiliary_count = 0
    for key, members in groups.items():
        rates = [number(p['bpp']) for p in members]
        aps = [number(p['box_ap']) for p in members]
        result_groups[key] = dict(count=len(members), bpp_min=min(rates),
                                 bpp_max=max(rates), ap_min=min(aps), ap_max=max(aps))
        if key[0] in ('c3', 'c3_prime') or key[1] == 'float32':
            primary_ranges.append((min(rates), max(rates)))
            primary_count += len(members)
        else:
            auxiliary_count += len(members)
    return dict(groups=result_groups, primary_count=primary_count,
                auxiliary_count=auxiliary_count, baseline_count=1,
                baseline_bpp=None, baseline_ap=number(data['JPEG_baseline']['box_ap']),
                rate_overlap=(max(x[0] for x in primary_ranges),
                              min(x[1] for x in primary_ranges)))


def verify_normalized_summary(candidate, data, rows):
    """Compare semantic results, after adapting any chosen output presentation.

    This neither executes submitted code nor approves publication. Provenance,
    bounded filesystem collection, and human interpretation review remain separate.
    """
    expected = independent_summary(data, rows)
    for field in ('primary_count', 'auxiliary_count', 'baseline_count', 'baseline_bpp'):
        require(type(candidate[field]) is type(expected[field])
                and candidate[field] == expected[field], 'incorrect ' + field)
    require(number(candidate['baseline_ap']) == expected['baseline_ap'], 'baseline AP mismatch')
    require(tuple(map(number, candidate['rate_overlap'])) == expected['rate_overlap'],
            'incorrect common rate range')
    require(candidate['groups'].keys() == expected['groups'].keys(), 'incorrect grouping')
    for key, values in expected['groups'].items():
        actual = candidate['groups'][key]
        require(type(actual['count']) is int and actual['count'] == values['count'],
                'incorrect group count')
        for metric in ('bpp_min', 'bpp_max', 'ap_min', 'ap_max'):
            require(number(actual[metric]) == values[metric], 'incorrect group range')


class GoalResearchAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.data, self.rows = read_public_inputs()

    def test_public_inputs_match_and_recomputed_ranges_match_independent_golden(self):
        actual = independent_summary(self.data, self.rows)
        self.assertEqual(actual['primary_count'], 16)
        self.assertEqual(actual['auxiliary_count'], 8)
        self.assertEqual(actual['baseline_count'], 1)
        self.assertIsNone(actual['baseline_bpp'])
        self.assertEqual(actual['baseline_ap'], D('47.432959401770944'))
        self.assertEqual(actual['rate_overlap'],
                         (D('0.11394990474969535'), D('0.27705573918739584')))
        for key, ranges in EXPECTED_RANGES.items():
            self.assertEqual(actual['groups'][key], dict(
                zip(('count', 'bpp_min', 'bpp_max', 'ap_min', 'ap_max'),
                    (4,) + tuple(map(D, ranges)))))

    def test_precomputed_range_metadata_is_not_used_as_the_oracle(self):
        altered = deepcopy(self.data)
        altered['rate_ranges'] = {'wrong': [0, 100]}
        altered['four_way_rate_overlap'] = [0, 100]
        self.assertEqual(independent_summary(altered, self.rows),
                         independent_summary(self.data, self.rows))

    def test_shared_metrics_and_point_identity_are_checked(self):
        for mutation in ('changed_ap', 'duplicate', 'missing', 'nonfinite'):
            with self.subTest(mutation=mutation):
                rows = deepcopy(self.rows)
                if mutation == 'changed_ap': rows[0]['box_ap'] = '99'
                if mutation == 'duplicate': rows[1] = deepcopy(rows[0])
                if mutation == 'missing': rows.pop(0)
                if mutation == 'nonfinite': rows[0]['bpp'] = 'NaN'
                with self.assertRaises(ResearchAcceptanceError):
                    independent_summary(self.data, rows)

    def test_jpeg_missing_rate_is_not_zero_or_a_codec_point(self):
        rows = deepcopy(self.rows)
        next(row for row in rows if row['method'] == 'JPEG_baseline')['bpp'] = '0'
        with self.assertRaisesRegex(ResearchAcceptanceError, 'JPEG'):
            independent_summary(self.data, rows)

    def test_candidate_success_cannot_mask_wrong_research_content(self):
        valid = independent_summary(self.data, self.rows)
        verify_normalized_summary(valid, self.data, self.rows)
        for mutation in ('omit_precision', 'zero_baseline', 'wrong_ap', 'wrong_count', 'wrong_overlap'):
            with self.subTest(mutation=mutation):
                candidate = deepcopy(valid)
                candidate['reported_outcome'] = 'success'  # Untrusted declaration.
                if mutation == 'omit_precision': candidate['groups'].pop(('transtic', 'uint8'))
                if mutation == 'zero_baseline': candidate['baseline_bpp'] = 0
                if mutation == 'wrong_ap': candidate['groups'][('c3', 'uint8')]['ap_max'] = 100
                if mutation == 'wrong_count': candidate['primary_count'] = 24
                if mutation == 'wrong_overlap': candidate['rate_overlap'] = (0, 1)
                with self.assertRaises(ResearchAcceptanceError):
                    verify_normalized_summary(candidate, self.data, self.rows)

    def test_reading_and_oracle_leave_public_input_bytes_unchanged(self):
        before = (PUBLIC_JSON.read_bytes(), PUBLIC_CSV.read_bytes())
        independent_summary(self.data, self.rows)
        self.assertEqual(before, (PUBLIC_JSON.read_bytes(), PUBLIC_CSV.read_bytes()))


if __name__ == '__main__':
    unittest.main()
