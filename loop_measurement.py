from collections import defaultdict
from datetime import datetime


CHECKER_VERSION = 'aggregate-answer-v1'
LIMITATIONS = [
    'Eligibility is observed starters inside the entry window, not lifetime-new workspaces.',
    'Events received after as_of are excluded; later delivery can change a new observation.',
    'Retention and authorized erasure can remove evidence; aggregate receipts cannot restore raw records.',
    'This rubric checks faithful aggregate interpretation, not subjective answer usefulness.',
]


def event_matches(row, rule):
    if row['name'] != rule['event']:
        return False
    properties = row['properties']
    for condition in rule.get('filters', []):
        key = condition['property']
        if key not in properties:
            return False
        values = condition['value'] if condition.get('op', 'eq') == 'in' else [condition['value']]
        value = properties[key]
        if not any(value == expected and (type(value) is type(expected) or
                   type(value) in (int, float) and type(expected) in (int, float)) for expected in values):
            return False
    return True


def eligible(row, environment):
    properties = row['properties']
    return (bool(row['distinct_id']) and row['name'] not in {'analytics.pipeline', 'afterspan.setup.checked'}
            and properties.get('environment') == environment and properties.get('internal') is not True
            and properties.get('subject_kind') not in {'anonymous', 'internal'})


def count_first_entry_outcomes(rows, measurement, window, assessments, environment):
    start, end = window['entry_start'], window['entry_end']
    through, as_of = window['observed_through'], window['as_of']
    by_identity = defaultdict(list)
    seen = set()
    anonymous = 0
    for row in sorted(rows, key=lambda item: (item['ts'], item['id'])):
        if row['id'] in seen or row['received'] > as_of or row['ts'] > through:
            continue
        seen.add(row['id'])
        if not row['distinct_id']:
            anonymous += 1
        if eligible(row, environment):
            by_identity[row['distinct_id']].append(row)
    result = dict.fromkeys(['entrants', 'mature', 'waiting', 'qualified', 'failed', 'unassessed',
                           'timed_out', 'technical_failures', 'completion_events_observed', 'observed_starters',
                           'release_excluded_starters', 'release_missing_starters', 'release_mismatched_completions',
                           'release_missing_completions'], 0)
    for events in by_identity.values():
        entry = next((row for row in events if start <= row['ts'] < end
                      and event_matches(row, measurement['entry'])), None)
        if entry is None:
            continue
        result['observed_starters'] += 1
        release = window.get('release')
        if release and entry['properties'].get('release') != release:
            key = 'release_excluded_starters' if entry['properties'].get('release') else 'release_missing_starters'
            result[key] += 1
            continue
        result['entrants'] += 1
        deadline = entry['ts'] + measurement['followup_seconds']
        followup = [row for row in events if entry['ts'] < row['ts'] <= min(deadline, through)]
        result['technical_failures'] += sum(row['properties'].get('technical_failure') is True for row in followup)
        completions = [row for row in followup if event_matches(row, measurement['completion'])]
        if release:
            result['release_mismatched_completions'] += sum(bool(row['properties'].get('release')) and row['properties']['release'] != release for row in completions)
            result['release_missing_completions'] += sum(not row['properties'].get('release') for row in completions)
            completions = [row for row in completions if row['properties'].get('release') == release]
        result['completion_events_observed'] += len(completions)
        if deadline > through:
            result['waiting'] += 1
            continue
        result['mature'] += 1
        if not completions:
            result['timed_out'] += 1
            continue
        verdicts = [set(assessments.get(row['id'], [])) for row in completions]
        if any(len(values) != 1 for values in verdicts):
            state = 'unassessed'
        elif any(values == {'qualified'} for values in verdicts):
            state = 'qualified'
        else:
            state = 'failed'
        result[state] += 1
    mature = result['mature']
    lower_bound = result['qualified'] / mature if mature else None
    result.update(rate=lower_bound if not result['unassessed'] else None,
                  confirmed_lower_bound=lower_bound, eligibility='observed_starters', unit=measurement['unit'],
                  anonymous_events_excluded=anonymous)
    return result


def next_action(result):
    if result['unassessed'] or result['failed'] or result['timed_out']:
        return 'review_answers'
    if not result['entrants']:
        return 'collect_evidence'
    if result['waiting']:
        return 'keep_observing'
    return 'no_change'


def check_answer(answer, evidence):
    fields = ['source', 'measurement_revision', 'mature', 'qualified', 'waiting', 'unassessed',
              'rate', 'limitations', 'next_action']
    mismatches = [field for field in fields if answer[field] != evidence[field]]
    return {'verdict': 'failed' if mismatches else 'qualified', 'mismatches': mismatches,
            'checker_version': CHECKER_VERSION}


def compare(before, after, release=None):
    reasons = []
    for field in ['measurement_revision', 'mapping_revision', 'source', 'environment']:
        if before[field] != after[field]:
            reasons.append(field + '_changed')
    windows = before.get('window') is not None and after.get('window') is not None
    if not windows:
        reasons.append('observation_window_unavailable')
    elif datetime.fromisoformat(before['window']['observed_through']) > datetime.fromisoformat(after['window']['entry_start']):
        reasons.append('entry_windows_overlap_or_reversed')
    for label, observation in [('before', before), ('after', after)]:
        if not observation['coverage']['complete'] or observation['waiting']:
            reasons.append(label + '_coverage_incomplete')
        if observation['mature'] is None:
            reasons.append(label + '_denominator_unavailable')
        elif observation['mature'] == 0:
            reasons.append(label + '_zero_denominator')
        elif observation['rate'] is None:
            reasons.append(label + '_quality_unassessed')
    release_reason = 'no_verified_release_receipt'
    release_bound = False
    descriptive_difference = after['rate'] - before['rate'] if not reasons else None
    if release and windows:
        released_at = release.get('released_at')
        if isinstance(released_at, str):
            released_at = datetime.fromisoformat(released_at).timestamp()
        if (before['window'].get('release') != release['base_commit']
                or after['window'].get('release') != release['commit']):
            release_reason = 'observation_release_not_bound_to_action'
        elif (released_at is not None and datetime.fromisoformat(before['window']['observed_through']).timestamp()
                <= released_at <= datetime.fromisoformat(after['window']['entry_start']).timestamp()):
            release_bound = True
            release_reason = None
        else:
            release_reason = 'release_does_not_separate_observation_windows'
    if release_reason:
        reasons.append(release_reason)
    comparable = not reasons
    return {'status': 'comparable' if comparable else 'unknown', 'reasons': reasons,
            'before': before['id'], 'after': after['id'], 'observational': True,
            'rate_difference': after['rate'] - before['rate'] if comparable else None,
            'unbound_rate_difference': descriptive_difference,
            'post_release_comparison': comparable and release_bound,
            'release_limitation': release_reason,
            'claim': 'Observed difference only; this comparison does not establish causation.',
            'environment': after['environment'], 'source': after['source']}
