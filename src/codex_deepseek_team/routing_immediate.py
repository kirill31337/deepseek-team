"""Default admission based on task suitability, independently of capacity."""
from .routing_models import READ_KINDS, WRITE_KINDS

FAMILY_FIELDS = ('kind', 'domain', 'operation', 'runtime', 'model', 'effort', 'context_version')
REWORK_LIMIT = 3


def ineligible_reason(features, access):
    if features['kind'] not in READ_KINDS | WRITE_KINDS:
        return 'protected_coordinator_task'
    if features['kind'] in WRITE_KINDS and access != 'full-access':
        return 'write_access_required'
    requirements = {
        'risk': ('low', 'medium'), 'scope_size': ('small', 'medium'),
        'coupling': ('local', 'component'), 'localization': ('known', 'partial'),
        'clarity': ('clear',),
    }
    for key, allowed in requirements.items():
        if features[key] not in allowed:
            return 'ineligible_' + key
    if features['verification'] not in ('tests', 'reproducer'):
        manual = (features['verification'] == 'manual'
                  and features['kind'] in READ_KINDS | {'documentation'})
        if not manual:
            return 'ineligible_verification'
    if any(features[key] == 'unknown' for key in ('domain', 'operation', 'context_version')):
        return 'unknown_context'
    if features['model'] != 'deepseek-flash':
        return 'ineligible_model'
    return None


def manual_acceptance(item):
    """Human-verifiable read/docs work still needs explicit acceptance criteria."""
    return (item['kind'] in READ_KINDS | {'documentation'}
            and item['features']['verification'] == 'manual'
            and any(isinstance(text, str) and text.strip() for text in item['acceptance']))


def should_pause(row, observations, config):
    """Keep quality history intact; distinguish a repair from repeated failures."""
    if row['outcome'] == 'rejected':
        return True
    if row['outcome'] != 'rework':
        return False
    family = tuple(row['features'][key] for key in FAMILY_FIELDS)
    cutoff = row['observed_at'] - config['recovery_cooldown_seconds']
    cases = {other['case_id'] for other in observations
             if other['origin'] == 'local' and other['action'] == 'worker'
             and other['outcome'] == 'rework'
             and cutoff <= other['observed_at'] <= row['observed_at']
             and tuple(other['features'][key] for key in FAMILY_FIELDS) == family}
    return len(cases) >= REWORK_LIMIT
