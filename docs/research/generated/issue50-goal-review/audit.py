#!/usr/bin/env python3
"""Standard-library audit of the three public Issue50 inputs; no model evaluation."""
import argparse
import csv
import hashlib
import io
import itertools
import json
import math
from pathlib import Path

SOURCE_SHA = 'a3c285fb191f95bdf3ecea54408c53adb177b5c1'
SCOPE = Path('docs/research/generated/issue50-goal-review')
INPUTS = ['docs/research/assets/issue50_four_method.json',
          'docs/research/assets/issue50_four_method.csv',
          'docs/research/2026-10-16_four_method_comparison.md']
# SHA256 of the exact Git blob bytes at SOURCE_SHA, not the current checkout.
EXPECTED_INPUT_SHA256 = {
    INPUTS[0]: '90bc064100ddda3c39a5eff8b03912bfc47435ae3e183c6bcafcbd4caabacc8b',
    INPUTS[1]: '96cbd38c693642149d8a7acec694e533e5e800015943db31129d33d3bf3078bf',
    INPUTS[2]: '5e8bfb54753743d3fcc634377377c80dd247541db64c6dba16cdadbceac916f0',
}
AP = ['box_ap', 'box_ap50', 'box_ap75', 'box_ap_small', 'box_ap_medium', 'box_ap_large']
SENS = ['delete_one_AP_min', 'delete_one_AP_max']
METHODS = ['c3', 'c3_prime', 'transtic', 'adapt_icmh']
EXPECTED = {(m, q, p) for m in METHODS
            for q in ([100, 150, 225, 300] if m.startswith('c3') else [1, 2, 3, 4])
            for p in (['uint8'] if m.startswith('c3') else ['float32', 'uint8'])}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def load_inputs(root):
    """Verify the approved snapshot and parse those same bytes, without rereading."""
    contents, provenance = {}, []
    for path in INPUTS:
        raw = (root / path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected = EXPECTED_INPUT_SHA256[path]
        require(digest == expected,
                f'input snapshot mismatch: {path}; expected SHA256 {expected} '
                f'at {SOURCE_SHA}, got {digest}')
        contents[path] = raw
        provenance.append({'path': path, 'sha256': digest, 'source_sha': SOURCE_SHA})
    data = json.loads(contents[INPUTS[0]].decode('utf-8'))
    rows = list(csv.DictReader(io.StringIO(contents[INPUTS[1]].decode('utf-8'), newline='')))
    return data, rows, provenance


def number(value, label):
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError('missing/invalid number: ' + label) from None
    require(math.isfinite(n), 'nonfinite number: ' + label)
    return n


def key(row):
    try:
        q = int(str(row['quality']))
        return row['method'], q, row['precision']
    except (KeyError, ValueError, TypeError):
        raise ValueError('invalid method/quality/precision key') from None


def index(rows, label):
    out = {}
    for row in rows:
        k = key(row)
        require(k not in out, label + ': duplicate key ' + str(k))
        out[k] = row
    require(set(out) == EXPECTED, label + ': missing or unexpected keys')
    return out


def interval(rows, field):
    values = [number(r[field], field) for r in rows]
    return [min(values), max(values)]


def overlap(ranges):
    lo, hi = max(r[0] for r in ranges), min(r[1] for r in ranges)
    return [lo, hi] if lo <= hi else None


def audit(data, rows):
    require(data.get('image_count') == 20, 'expected fixed 20-image dataset')
    j = index(data['points'], 'JSON')
    baseline = [r for r in rows if r.get('method') == 'JPEG_baseline']
    require(len(rows) == 25 and len(baseline) == 1, 'expected 25 CSV rows including one JPEG')
    c = index([r for r in rows if r.get('method') != 'JPEG_baseline'], 'CSV')
    for k in sorted(EXPECTED):
        for field in ['bpp'] + AP + SENS:
            a, b = number(j[k].get(field), field), number(c[k].get(field), field)
            require(a == b, 'JSON/CSV mismatch: ' + str(k) + ' ' + field)
            require(a > 0 if field == 'bpp' else 0 <= a <= 100, 'out-of-range ' + field)
        require(j[k][SENS[0]] <= j[k][SENS[1]], 'reversed sensitivity range')
    b = baseline[0]
    require(b.get('quality') == '' and b.get('precision') == 'original_JPEG', 'invalid JPEG identity')
    require(b.get('bpp') == '' and data['JPEG_baseline'].get('bpp') is None, 'JPEG bpp must remain missing')
    for field in AP:
        require(number(b.get(field), field) == number(data['JPEG_baseline'].get(field), field), 'JPEG mismatch ' + field)
    require(number(b['box_ap'], 'JPEG AP') == 47.432959401770944, 'unexpected JPEG AP')
    bs = [number(b.get(f), f) for f in SENS]
    require(0 <= bs[0] <= bs[1] <= 100, 'invalid JPEG sensitivity')
    groups = {}
    for m, p in sorted({(k[0], k[2]) for k in EXPECTED}):
        points = [j[k] for k in sorted(j) if k[0] == m and k[2] == p]
        groups[m + '/' + p] = {'count': len(points), 'bpp_range': interval(points, 'bpp'), 'AP_range_percent': interval(points, 'box_ap')}
    primary = {m: groups[m + ('/uint8' if m.startswith('c3') else '/float32')]['bpp_range'] for m in METHODS}
    require(primary == data['rate_ranges'], 'reported rate ranges mismatch')
    pairs = {a + ' / ' + b: overlap([primary[a], primary[b]]) for a, b in itertools.combinations(METHODS, 2)}
    require(pairs == data['pairwise_rate_range_overlap'], 'reported pairwise overlap mismatch')
    common = overlap(list(primary.values()))
    require(common == data['four_way_rate_overlap'], 'reported four-way overlap mismatch')
    deltas = []
    for m in ['adapt_icmh', 'transtic']:
        for q in range(1, 5):
            f, u = j[m, q, 'float32'], j[m, q, 'uint8']
            require(f['bpp'] == u['bpp'], 'precision pair rate mismatch')
            deltas.append({'method': m, 'quality': q, 'bpp': f['bpp'], 'float32_AP': f['box_ap'], 'uint8_AP': u['box_ap'], 'uint8_minus_float32_AP_pp': u['box_ap'] - f['box_ap']})
    return {'audit': {'status': 'pass', 'compressed_points': 24, 'csv_rows': 25, 'method_precision_groups': 6,
                       'primary_points': 16, 'supplementary_external_uint8_points': 8, 'JPEG_rows': 1,
                       'all_uint8_reused_C3_points': 8, 'new_measurements_this_run': 0,
                       'comparison': 'exact numeric equality after float parsing; all six AP fields, bpp and shared delete-one extrema'},
            'groups': groups, 'primary_rate_ranges': primary, 'pairwise_rate_overlap': pairs,
            'four_way_rate_overlap': common, 'precision_AP_deltas': deltas,
            'JPEG': {'AP_percent': float(b['box_ap']), 'bpp': None,
                     'CSV_only_delete_one_AP_range': bs, 'CSV_only_range_cross_checked_with_JSON': False},
            'sensitivity': {'shared_point_extrema_cross_checked': 24, 'raw_delete_one_values_available': False,
                            'paired_difference_ranges_source_only_not_recomputed': data['paired_delete_one_uint8_minus_float_AP_pp_ranges'],
                            'confidence_interval': False},
            'historical_metadata_not_new_measurements': {'category_support_range': data['category_support_range'],
                                                       'costs': data['costs'], 'codec_alignment': data['codec_alignment']}}


def report(s):
    lines = ['# Issue50 public aggregate audit (Issue #87)', '',
             'CPU-only reaggregation of the three approved public inputs. No new images, splits, calibration, training, codec inference or detector evaluation. This is an internal consistency audit, not independent scientific validation.', '',
             '## Results', '',
             'PASS: all three public inputs match the SHA256 digests pinned to the approved source snapshot. 24 compressed points, 25 CSV rows including JPEG, six method/precision groups of four points. No missing keys, duplicate keys or numeric mismatches found. Primary comparison: 16 points; external uint8 supplement: 8 points. The all-uint8 view reuses the eight C3/C3′ points; it adds no measurements.', '',
             '| Group | Points | bpp endpoints | AP range (%) |', '|---|---:|---|---|']
    for k, v in s['groups'].items():
        lines.append(f"| {k} | {v['count']} | {v['bpp_range']} | {v['AP_range_percent']} |")
    lines += ['', 'All six primary pairwise endpoint intersections:', '']
    lines += [f'- {k}: {v}' for k, v in s['pairwise_rate_overlap'].items()]
    lines += ['', f"Four-way endpoint intersection: {s['four_way_rate_overlap']} bpp. These are endpoint ranges, not continuously measured rates or matched-rate points.", '',
              '| Method | Quality | uint8 minus float32 AP (pp) |', '|---|---:|---:|']
    for d in s['precision_AP_deltas']:
        lines.append(f"| {d['method']} | {d['quality']} | {d['uint8_minus_float32_AP_pp']!r} |")
    lines += ['', 'Precision differences have both signs. External pairs use the same reconstruction and estimated bits; uint8 uses ties-to-even rounding after clamping. C3 float reconstructions are unavailable; no C3 float AP is inferred.', '',
              'JPEG AP remains 47.432959401770944%; bpp remains null (CSV blank), never zero. JPEG is the distributed lossy JPEG baseline, not a lossless original. Its CSV-only delete-one range is ' + str(s['JPEG']['CSV_only_delete_one_AP_range']) + '; JSON has no matching range, so no cross-check is claimed.', '',
              '## Definitions and limits', '',
              'bpp is the arithmetic mean of each image’s estimated bits divided by D2-resized, pre-alignment nonpadding pixels. External codecs include y+z likelihood bits; C3 includes latent plus per-image network bits. Shared pretrained external weights are not charged per image. These are estimates, not actual bitstreams or container sizes. Do not combine them with old Issue20 original-pixel/aggregate definitions.', '',
              'AP is official COCO bbox .50:.95 dataset AP (%) on the fixed 20 images, not a mean of per-image AP. Aggregate inputs cannot reconstruct bits, evaluate AP again, or recompute delete-one/jackknife values. Shared JSON/CSV delete-one extrema were checked for consistency only. Paired difference ranges are retained as source metadata; subtracting marginal extrema cannot recover paired differences. Delete-one sensitivity is descriptive, not a confidence interval or significance test.', '',
              'Small sample size and 26–30 supported categories limit interpretation. Four discrete rates per group do not establish same-rate superiority. No AP interpolation, extrapolation, BD-rate, generalization or independent confirmation claim is made. TransTIC uses official 256-multiple bilinear resize before/after codec (align_corners=False); Adapt-ICMH pads right/bottom then crops; C3 uses native detector-sized inputs. Alignment and primary external float32 versus C3 uint8 precision remain confounded.', '',
              'The historical report records fixed evaluation images and nonoverlap with a calibration set of 20 images. No new split or image selection was performed here; aggregate data cannot independently verify nonoverlap, historical image hashes, model behavior or pretraining overlap. The three public aggregate/context files are verified byte-for-byte against the approved source snapshot. The historical 188.23444253765047 GPU seconds are not a cost of this run. This run used no GPU and installed no dependencies.', '',
              '## Reproduction and provenance', '',
              'Run from the repository root with Python 3 and only the standard library:', '',
              '```sh', 'python -B docs/research/generated/issue50-goal-review/test_audit.py',
              'python -B docs/research/generated/issue50-goal-review/audit.py', '```', '',
              'The CLI accepts --root and --output (output must stay inside the approved scope). JSON is sorted, timestamp-free, rejects NaN and is deterministic for identical inputs and test evidence. Before parsing or writing outputs, the CLI compares all three input SHA256 digests against constants derived from the exact Git blob bytes at the approved source SHA below, then parses those same verified bytes. Any change, including coherent JSON/CSV edits or Markdown-only edits, aborts without writing output. Source SHA below is the approved input snapshot, not the historical codec runtime SHA. The markdown input supplies historical context; rounded prose tables are not treated as full-precision independent measurements.', '',
              f"Approved input source SHA: `{SOURCE_SHA}`.", '', '| Input | SHA256 |', '|---|---|']
    lines += [f"| {x['path']} | `{x['sha256']}` |" for x in s['inputs']]
    t = s['tests']
    lines += ['', '## Validation, failures and negative results', '',
              f"unittest: {t['tests_run']} tests; {t['failures']} failures; {t['errors']} errors; {t['skipped']} skipped. See test-results.json for exact names and outcomes. Negative fixtures deliberately exercise missing points/fields, duplicate keys, mismatched metrics, invalid rates/precision keys, nonfinite numbers, altered overlaps, invalid JPEG data, coordinated JSON/CSV drift, and Markdown-only drift. CLI snapshot tests also check rejection without creating or overwriting outputs. Expected rejections are successful tests, not research failures.", '',
              'During original report generation, git and rg were unavailable; initial read-only discovery commands returned command-not-found. Python filesystem inspection replaced them. Snapshot-verification regression tests use only copies of these public inputs and Python standard-library code. No dependencies were installed. No scientific evaluation failed because none was attempted. No publication retry or extra research run is part of this workflow.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument('--output', type=Path, default=SCOPE)
    args = parser.parse_args()
    root = args.root.resolve()
    out = (root / args.output).resolve()
    require(out.is_relative_to((root / SCOPE).resolve()), 'output outside approved scope')
    data, rows, inputs = load_inputs(root)
    s = audit(data, rows)
    s['inputs'] = inputs
    s['tests'] = json.loads((root / SCOPE / 'test-results.json').read_text())
    require(s['tests']['failures'] == s['tests']['errors'] == 0, 'tests did not pass')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.json').write_text(json.dumps(s, indent=2, sort_keys=True, allow_nan=False) + '\n')
    (out / 'report.md').write_text(report(s))
    print('PASS: 24 compressed points / 25 CSV rows; summary.json and report.md generated')


if __name__ == '__main__':
    main()
