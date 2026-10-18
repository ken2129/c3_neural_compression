"""Closed approved-goal data contract; validation never authorizes execution.

The owner receipt binds the complete canonical plan, including instructions and
resource limits. Source paths name files in the pinned, operator-installed
revision. A trusted runner must stage them and enforce the declared write and
resource boundaries before dispatch. This module performs no I/O or model calls.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .schema import ContractError, canonical, fields, relative_path

GOAL_KIND = 'codex-goal-v1'
DIRECT_PR_KIND = 'codex-direct-pr-v1'
MAX_PLAN_BYTES = 16384
# Example requested budget only; this is neither deployment configuration nor
# proof of enforcement. CPU is a per-process limit.
GOAL_RESOURCE_BUDGET = {
    'cpu_seconds_per_process': 120,
    'memory_bytes': 1024 * 1024 * 1024,
    'max_processes': 64,
    'max_work_bytes': 256 * 1024 * 1024,
    'max_output_bytes': 1024 * 1024,
}
# Parser bounds prevent pathological requests; they do not authorize resource
# expansion. Runtime admission additionally requires a verified operator profile
# and enforcement of every requested limit (not merely these parser ceilings).
RESOURCE_BOUNDS = {
    'max_seconds': (1, 3600),
    'cpu_seconds_per_process': (1, 3600),
    'memory_bytes': (64 * 1024 * 1024, 16 * 1024 * 1024 * 1024),
    'max_processes': (1, 1024),
    'max_work_bytes': (1024 * 1024, 16 * 1024 * 1024 * 1024),
    'max_output_bytes': (1, 16 * 1024 * 1024),
}
_PLAN_FIELDS = (
    'kind', 'deployment_id', 'source_sha', 'publish_result_pr', 'model', 'sandbox_mode',
    'external_network', 'max_seconds', 'model_usage_approved', 'goal',
    'completion_criteria', 'inputs', 'write_scope', 'resource_budget',
)


def _text(value, *, maximum):
    if type(value) is not str or not value.strip():
        raise ContractError('nonempty goal text required')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as exc:
        raise ContractError('invalid goal text encoding') from exc
    if size > maximum or any(ord(char) < 32 and char not in '\n\t' for char in value):
        raise ContractError('goal text outside bounds')
    return value


def _paths(value, *, maximum, empty=False):
    if type(value) is not list or not (0 if empty else 1) <= len(value) <= maximum:
        raise ContractError('bounded goal path list required')
    seen = set()
    for path in value:
        relative_path(path)
        try:
            size = len(path.encode('utf-8'))
        except UnicodeError as exc:
            raise ContractError('invalid goal path encoding') from exc
        if (size > 256
                or any(part.startswith('.') for part in path.split('/'))
                or any(char in path for char in '*?[]') or path in seen):
            raise ContractError('unsafe or duplicate goal path')
        seen.add(path)
    return value


def validate_goal_plan(plan, allowed_source_sha, *, resource_limits=None, deployment_id=None):
    """Return a detached validated plan; callers still need trusted approval.

    Path validation is lexical only. Staging must reject symlinks/special files
    and ensure source bytes come from the approved revision. A path list and a
    prompt do not enforce a filesystem boundary. With resource_limits omitted,
    only syntax and absolute parser bounds are checked. A supplied limits map
    contains all six RESOURCE_BOUNDS keys and represents operator ceilings, not
    evidence that a supervisor enforces the requested resource budget. The public
    deployment nonce is always required; executable admission must additionally
    match it to the protected operator profile before making a durable claim.
    """
    direct = type(plan) is dict and plan.get('kind') == DIRECT_PR_KIND
    fields(plan, (*_PLAN_FIELDS, 'direct_pr') if direct else _PLAN_FIELDS)
    if plan['kind'] not in (GOAL_KIND, DIRECT_PR_KIND):
        raise ContractError('unsupported goal kind')
    if direct:
        from .schema import REPOSITORY
        target = plan['direct_pr']
        fields(target, ('repository', 'issue_number', 'base_branch', 'base_sha', 'title', 'public_results_approved'))
        if (target['repository'] != REPOSITORY or type(target['issue_number']) is not int
                or target['issue_number'] <= 0 or target['issue_number'] == 56
                or target['base_branch'] != 'main' or target['base_sha'] != allowed_source_sha
                or target['public_results_approved'] is not True):
            raise ContractError('explicit direct PR target/publication scope required')
        _text(target['title'], maximum=200)
        if not target['title'].startswith('[Codex] ') or '\n' in target['title']:
            raise ContractError('direct PR title must start with [Codex]')
    if type(plan['deployment_id']) is not str or re.fullmatch('[0-9a-f]{32}', plan['deployment_id']) is None:
        raise ContractError('public goal deployment identity required')
    if deployment_id is not None:
        if (type(deployment_id) is not str or re.fullmatch('[0-9a-f]{32}', deployment_id) is None
                or plan['deployment_id'] != deployment_id):
            raise ContractError('goal belongs to another deployment')
    if (type(allowed_source_sha) is not str
            or re.fullmatch(r'[0-9a-f]{40}', allowed_source_sha) is None
            or plan['source_sha'] != allowed_source_sha):
        raise ContractError('source is not the operator-installed approved revision')
    if (type(plan['model']) is not str
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', plan['model']) is None
            or plan['model'].endswith('-latest')):
        raise ContractError('explicit pinned goal model required')
    if (plan['publish_result_pr'] is not True
            or plan['model_usage_approved'] is not True
            or plan['sandbox_mode'] != 'externalSandbox'
            or plan['external_network'] != 'enabled'):
        raise ContractError('explicit bounded goal policy required')
    _text(plan['goal'], maximum=4096)
    criteria = plan['completion_criteria']
    if type(criteria) is not list or not 1 <= len(criteria) <= 16:
        raise ContractError('bounded completion criteria required')
    for criterion in criteria:
        _text(criterion, maximum=1024)
    if len(set(criteria)) != len(criteria):
        raise ContractError('duplicate completion criterion')
    _paths(plan['inputs'], maximum=32, empty=True)
    _paths(plan['write_scope'], maximum=8)
    budget = plan['resource_budget']
    fields(budget, GOAL_RESOURCE_BUDGET)
    requested = {'max_seconds': plan['max_seconds'], **budget}
    for key, (minimum, maximum) in RESOURCE_BOUNDS.items():
        if type(requested[key]) is not int or not minimum <= requested[key] <= maximum:
            raise ContractError('goal resource request outside parser bounds')
    if resource_limits is not None:
        fields(resource_limits, RESOURCE_BOUNDS)
        for key, (minimum, maximum) in RESOURCE_BOUNDS.items():
            ceiling = resource_limits[key]
            if type(ceiling) is not int or not minimum <= ceiling <= maximum:
                raise ContractError('invalid operator goal resource ceiling')
            if requested[key] > ceiling:
                raise ContractError('goal resource request exceeds operator ceiling')
    if len(canonical(plan)) > MAX_PLAN_BYTES:
        raise ContractError('goal plan exceeds size bound')
    return deepcopy(plan)


def direct_pr_scope(plan):
    """Installed input/write envelope; each new goal still needs owner approval."""
    if plan.get('kind') != DIRECT_PR_KIND:
        raise ContractError('direct PR plan required')
    return {key: deepcopy(plan[key]) for key in ('inputs', 'write_scope')}


def require_installed_goal(profile, plan):
    """Legacy installs pin one task; direct installs admit new approved goals."""
    import hashlib
    if plan.get('kind') == DIRECT_PR_KIND:
        if profile.get('direct_pr_scope') != direct_pr_scope(plan):
            raise ContractError('direct PR goal exceeds installed input/write envelope')
    elif ('direct_pr_scope' in profile
          or profile.get('installed_plan_digest') != hashlib.sha256(canonical(plan)).hexdigest()):
        raise ContractError('goal differs from prepared operator task')
