"""Freeze a COCO subset from explicit prior-use evidence, without reading AP.

Usage: python prepare_heldout.py --annotations ... --history ... --output ...
History arguments are saved Box MCP envelopes or plain manifests. Outputs stay
private: they include image IDs, source hashes and paths. This does not claim
coverage of unavailable experiment histories.
"""
import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def explicit_image_ids(value, *, root=False):
    if isinstance(value, list):
        if root and value and all(type(x) is int and x > 0 for x in value):
            return set(value)
        found = set()
        for item in value:
            found.update(explicit_image_ids(item))
        return found
    if not isinstance(value, dict):
        return set()
    found = set()
    # Box envelopes contain the original JSON as text, not as parsed fields.
    if value.get('type') == 'text' and isinstance(value.get('text'), str):
        try:
            found.update(explicit_image_ids(json.loads(value['text']), root=True))
        except ValueError:
            pass
    for key, item in value.items():
        if key in {'image_id', 'image_ids', 'img_ids', 'subset_ids'}:
            if type(item) is int and item > 0:
                found.add(item)
            elif isinstance(item, list) and all(type(x) is int and x > 0 for x in item):
                found.update(item)
        elif isinstance(item, (dict, list)):
            found.update(explicit_image_ids(item))
    return found


def prepare(annotations, history, output, n=20, seed=20261015):
    if n <= 1:
        raise ValueError('Need at least two images for paired delete-one analysis')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    sources = []; excluded = set()
    for path in history:
        ids = explicit_image_ids(json.loads(Path(path).read_text()), root=True)
        sources.append({'path': str(Path(path).resolve()), 'sha256': digest(path),
                        'explicit_prior_image_count': len(ids)})
        excluded.update(ids)
    annotation = json.loads(Path(annotations).read_text())
    by_id = {row['id']: row for row in annotation['images']}
    eligible = sorted(set(by_id) - excluded)
    if len(eligible) < n:
        raise ValueError('Insufficient verified eligible population')
    ranked = sorted(eligible, key=lambda i: (hashlib.sha256(f'{seed}:{i}'.encode()).digest(), i))
    selected = ranked[:n]
    assert not excluded.intersection(selected)
    result = {'schema_version': 1, 'status': 'selected_before_AP; input bytes pending',
              'seed': seed, 'N': n, 'image_ids': selected,
              'selection_rule': 'ascending SHA256(decimal_seed:decimal_image_id), then ID; no AP/category filtering',
              'population': 'COCO val2017 annotation image IDs minus explicit prior-use evidence',
              'population_count': len(by_id), 'eligible_count': len(eligible),
              'annotation_file': str(Path(annotations).resolve()), 'annotation_sha256': digest(annotations),
              'history_sources': sources, 'excluded_image_ids': sorted(excluded & set(by_id)),
              'coverage_limit': 'Unused relative to inspected experiment records; no assertion about unavailable/unrecorded use',
              'images': [by_id[i] for i in selected]}
    for name, value in [('selection.json', result), ('subset_ids.json', selected)]:
        (output/name).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--annotations', required=True); p.add_argument('--history', nargs='+', required=True)
    p.add_argument('--output', required=True); p.add_argument('--n', type=int, default=20)
    p.add_argument('--seed', type=int, default=20261015)
    a = p.parse_args(); result = prepare(a.annotations, a.history, a.output, a.n, a.seed)
    print(json.dumps({key: result[key] for key in ['N','eligible_count','population_count','status']}))
