"""Project service joining routing evidence, decisions and accounting."""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager, nullcontext
from copy import copy
import io
import time
import uuid

from . import routing_bootstrap, routing_estimator, routing_immediate, routing_recovery, settings
from .routing_models import (RoutingError, canonical, fingerprint, identifier, read_json,
                             number, validate_config, validate_features, validate_observation)
from .routing_store import RoutingStore, evidence_key


class RoutingService:
    def __init__(self, root: Path):
        try:
            self.root = settings.project_root(Path(root), required=True)
        except settings.SettingsError as error:
            raise RoutingError(str(error), 78) from None
        self.store = RoutingStore(self.root)
        self._connection = None

    def _transaction(self):
        return self.store.transaction() if self._connection is None else nullcontext(self._connection)

    @contextmanager
    def batch(self):
        """Keep related decisions and reservations in one caller-owned commit."""
        with self._transaction() as db:
            bound = copy(self)
            bound._connection = db
            yield bound

    @staticmethod
    def _config(db):
        row = db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
        return validate_config({} if row is None else read_json(row[0]))

    def config(self):
        with self._transaction() as db:
            return self._config(db)

    def configure(self, changes):
        with self._transaction() as db:
            config = validate_config(changes, base=self._config(db))
            db.execute("INSERT INTO metadata VALUES ('config', ?) ON CONFLICT(key) "
                       'DO UPDATE SET value=excluded.value', (canonical(config),))
            self.store.audit(db, 'configuration', 'config', config)
            return config

    def observations(self):
        with self._transaction() as db:
            return self.store.all(db, 'observations')

    def import_snapshot(self, data, source_id, source_url, sha256, reliability=.5):
        from .routing_sources import parse_snapshot
        parsed = parse_snapshot(data, source_id=source_id, source_url=source_url,
                                expected_sha256=sha256, reliability=reliability)
        metadata = dict(parsed['metadata'], reliability=number(reliability, 'reliability', 0, 1))
        version_id = fingerprint([source_id, sha256])
        with self._transaction() as db:
            previous = self.store.get(db, 'sources', version_id)
            if previous:
                metadata['imported_at'] = previous['imported_at']
            self.store.put(db, 'sources', version_id, metadata)
            added = sum(self.store.put(db, 'observations', row['id'], row)
                        for row in parsed['observations'])
        return {'source': metadata, 'added': added, 'duplicates': len(parsed['observations']) - added}

    def observe(self, record):
        row = validate_observation(record)
        if row['origin'] != 'local' or row['source_id'] != 'local' or row['source_family'] != 'local':
            raise RoutingError('Use pinned snapshot import for external observations.')
        if row['reliability'] != 1.:
            raise RoutingError('Local observations always retain full provenance reliability.')
        with self._transaction() as db:
            existing = self.store.get(db, 'observations', row['id'])
            if existing is not None:
                self.store.put(db, 'observations', row['id'], row)
                return row
            if row['decision_id']:
                decision = self.store.get(db, 'decisions', row['decision_id'])
                if decision is None or decision['features'] != row['features']:
                    raise RoutingError('Observation must match its original recorded feature card.')
                if row['observed_at'] < decision['created_at']:
                    raise RoutingError('An outcome cannot precede its recorded decision.')
                if (decision.get('recovery') or {}).get('selected') and row['action'] != 'worker':
                    raise RoutingError('A worker trial requires worker outcome feedback.')
                case = db.execute('SELECT case_id FROM evidence_index WHERE decision_id=? LIMIT 1',
                                  (row['decision_id'],)).fetchone()
                if case and case[0] != row['case_id']:
                    raise RoutingError('One recorded decision cannot represent different observed cases.')
            latest = db.execute('SELECT MAX(observed_at) FROM evidence_index WHERE case_key=?',
                                (evidence_key(row),)).fetchone()[0]
            if latest is not None and row['observed_at'] < latest:
                raise RoutingError('Local outcomes cannot be backdated before an existing case event.')
            self.store.put(db, 'observations', row['id'], row)
            routing_recovery.initialize(db)
            if row['action'] == 'worker' and row['outcome'] in ('rework', 'rejected'):
                config = self._config(db)
                if (config['admission_policy'] == 'evidence'
                        or routing_immediate.should_pause(row, self.store.all(db, 'observations'), config)):
                    routing_recovery.record_failure(db, row['features'], config, now=row['observed_at'])
            if row['decision_id'] and (decision.get('recovery') or {}).get('selected'):
                # Immediate admission owns its proportional failure policy,
                # including feedback from trials admitted before an upgrade.
                routing_recovery.finish(db, decision['recovery']['ticket_id'], row['outcome'],
                    now=row['observed_at'], start_cooldown=self._config(db)['admission_policy'] == 'evidence')
        return row

    def predict(self, features, access=None, record=True, *, binding=None, recovery=False):
        features = validate_features(features)
        try:
            policy = settings.resolve(self.root)
        except settings.SettingsError as error:
            raise RoutingError(str(error), 78) from None
        if access not in (None, 'read-only', 'full-access'):
            raise RoutingError('Routing access must be read-only or full-access.')
        access = 'read-only' if 'read-only' in (access, policy.effective_access) else 'full-access'
        if binding is not None:
            if not isinstance(binding, dict) or set(binding) != {'task_id', 'deliverable_id', 'plan_hash'}:
                raise RoutingError('Invalid plan decision binding.')
            for key, value in binding.items():
                identifier(value, key)
        with self._transaction() as db:
            config = self._config(db)
            now = time.time()
            routing_recovery.initialize(db)
            from .routing_models import WRITE_KINDS
            automatic = (policy.enabled and policy.delegation_level == 'auto' and config['mode'] == 'auto')
            if record and binding is not None:
                previous = routing_bootstrap.bound_decision(db, binding, features)
                if previous is not None:
                    # A revocation can narrow a frozen plan, never promote a
                    # denied/expired trial into a new worker opportunity.
                    writable = features['kind'] not in WRITE_KINDS or access == 'full-access'
                    if previous['action'] != 'worker' or (policy.enabled and writable and config['mode'] != 'off'
                            and (not previous.get('recovery', {}).get('selected') or automatic)):
                        return previous
            decision = routing_estimator.recommend(features, self.store.all(db, 'observations'),
                        config, access=access, enabled=policy.enabled, now=now)
            decision['estimated_action'] = decision['action']
            assessment = routing_bootstrap.assess(decision, config)
            decision['phase'] = assessment['phase']
            decision['phase_evidence'] = assessment
            immediate = automatic and config['admission_policy'] == 'immediate'
            if immediate:
                reason = routing_immediate.ineligible_reason(features, access)
                if binding is not None and not recovery:
                    reason = reason or 'declared_verification_required'
                decision['evidence_reason_codes'] = decision['reason_codes']
                decision['phase'] = 'immediate'
                decision['action'] = 'coordinator' if reason else 'worker'
                decision['reason_codes'] = [reason or 'immediate_eligible']
            if automatic:
                if routing_recovery.cooling(db, features, now=now):
                    decision['phase'] = 'recovery'
                    decision['phase_evidence'].update(phase='recovery', cooldown_active=True)
                    decision['action'] = 'coordinator'
                    decision['reason_codes'] = ['quality_failure_cooldown', *decision['reason_codes']]
                if assessment['economic_veto']:
                    decision['action'] = 'coordinator'
                    decision['reason_codes'] = ['insufficient_measured_savings',
                        *(r for r in decision['reason_codes'] if r != 'insufficient_measured_savings')]
            if (recovery and record and binding is not None and automatic and not immediate
                    and decision['action'] != 'worker'
                    and not assessment['economic_veto']
                    and (features['kind'] not in WRITE_KINDS or access == 'full-access')):
                admission = 'recovery' if decision['phase'] == 'recovery' else 'bootstrap'
                info = routing_recovery.consider(db, dict(config, profile=policy.delegation_level,
                    access=access, recommendation=decision['action']), features, binding, now=now,
                    admission=admission)
                decision['recovery'] = info
                if info['selected']:
                    decision['action'] = 'worker'
                    reason = ('controlled_recovery' if admission == 'recovery' else
                              'bounded_cost_learning' if decision['phase'] == 'adaptive' else 'active_bootstrap')
                    decision['reason_codes'] = [reason, *decision['reason_codes']]
            evidence_ids = decision['posterior'].get('evidence_ids', [])
            decision['posterior'].update(evidence_ids=evidence_ids[:128], evidence_count=len(evidence_ids),
                                         evidence_digest=fingerprint(evidence_ids))
            decision.update(id='decision-' + uuid.uuid4().hex, created_at=now,
                            features=features, feature_hash=fingerprint(features),
                            config=config, mode=config['mode'], access=access,
                            enabled=policy.enabled, algorithm_version=2, binding=binding)
            if record:
                self.store.put(db, 'decisions', decision['id'], decision)
                if binding is not None:
                    routing_bootstrap.bind_decision(db, decision)
            return decision

    def decision(self, decision_id):
        identifier(decision_id, 'decision_id')
        with self._transaction() as db:
            result = self.store.get(db, 'decisions', decision_id)
        if result is None:
            raise RoutingError('Unknown routing decision.', 66)
        return result

    def validate_plan_decision(self, decision_id, features, binding, *, executor=None):
        try:
            decision = self.decision(decision_id)
            valid = (decision['mode'] == 'auto' and decision['binding'] == binding
                     and decision['features'] == validate_features(features))
            if executor is not None:
                valid = valid and executor == ('worker' if decision['action'] == 'worker' else 'coordinator')
                if executor == 'worker':
                    from .routing_models import PROTECTED_KINDS, WRITE_KINDS
                    valid = (valid and decision['enabled'] and features['kind'] not in PROTECTED_KINDS
                             and (features['kind'] not in WRITE_KINDS or decision['access'] == 'full-access'))
            return valid
        except RoutingError:
            return False

    def evaluate(self):
        with self._transaction() as db:
            config, observations = self._config(db), self.store.all(db, 'observations')
        return routing_estimator.evaluate(observations, config)

    def _budget(self, operation, *args):
        from . import routing_budget
        with self._transaction() as db:
            routing_budget.initialize(db)
            result = getattr(routing_budget, operation)(db, self._config(db), *args)
            if operation != 'status':
                self.store.audit(db, 'budget_' + operation, args[0], result)
            return result

    def reserve_experiment(self, reservation_id, amount_usd):
        return self._budget('reserve', reservation_id, amount_usd)

    def settle_experiment(self, reservation_id, actual_usd):
        return self._budget('settle', reservation_id, actual_usd)

    def release_experiment(self, reservation_id):
        return self._budget('release', reservation_id)

    def recovery_status(self):
        from . import routing_recovery
        with self._transaction() as db:
            routing_recovery.initialize(db)
            return routing_recovery.status(db, self._config(db))

    def start_recovery(self, decision_id, *, already_running=False, validate_only=False, access=None):
        from . import routing_recovery
        with self._transaction() as db:
            decision = self.store.get(db, 'decisions', decision_id)
            if decision is None:
                raise RoutingError('Unknown routing decision.', 66)
            info = (decision or {}).get('recovery') or {}
            routing_recovery.initialize(db)
            # A running ticket is an idempotent reconciliation, not a new
            # launch. Pending trials and ordinary automatic starts must still
            # observe failures learned after their plan was recorded.
            running = info.get('selected') and db.execute(
                'SELECT 1 FROM routing_recovery_tickets WHERE ticket_id=? AND status=?',
                (info['ticket_id'], 'running')).fetchone()
            reconciling_ordinary = already_running and not info.get('selected')
            if not running and not reconciling_ordinary and self._config(db)['mode'] != 'auto':
                raise RoutingError('Current routing mode does not permit a new automatic worker start.', 78)
            if not running and not reconciling_ordinary and routing_recovery.cooling(db, decision['features']):
                raise RoutingError('Task family is in a quality failure cooldown; inspect and replan later.', 78)
            if not running and not reconciling_ordinary:
                current = settings.resolve(self.root)
                current_access = access or current.effective_access
                if not current.enabled:
                    raise RoutingError('Delegation was disabled while the assignment waited.', 69)
                if decision['features']['kind'] in routing_immediate.WRITE_KINDS and current_access != 'full-access':
                    raise RoutingError('Current access does not permit this queued writing assignment.', 78)
                forecast = routing_estimator.recommend(decision['features'], self.store.all(db, 'observations'),
                                                       self._config(db), access=current_access)
                if routing_bootstrap.assess(forecast, self._config(db))['economic_veto']:
                    raise RoutingError('Measured economics no longer support this queued assignment.', 78)
            if info.get('selected') and validate_only:
                ticket = db.execute(
                    'SELECT status, expires_at FROM routing_recovery_tickets WHERE ticket_id=?',
                    (info['ticket_id'],)).fetchone()
                if (ticket is None or ticket[0] not in ('pending', 'running')
                        or (ticket[0] == 'pending' and ticket[1] <= time.time())):
                    raise RoutingError('Recovery ticket is no longer available; use a new reviewed task plan.', 78)
            elif info.get('selected'):
                result = routing_recovery.mark_started(db, info['ticket_id'])
                if not result['started']:
                    raise RoutingError('Recovery ticket is no longer available; use a new reviewed task plan.', 78)
                self.store.audit(db, 'recovery_started', info['ticket_id'], result)

    def release_recovery(self, ticket_id):
        from . import routing_recovery
        with self._transaction() as db:
            routing_recovery.initialize(db)
            result = routing_recovery.release(db, ticket_id)
            if result['status'] == 'running':
                raise RoutingError('Inspect and disposition the running worker; its recovery ticket cannot be released.', 78)
            if result['status'] is None:
                raise RoutingError('Unknown recovery ticket.', 66)
            self.store.audit(db, 'recovery_release', ticket_id, result)
            return result

    def status(self):
        with self._transaction() as db:
            config = self._config(db)
            counts = {'local': 0, 'external': 0}
            for (raw,) in db.execute('SELECT value FROM observations'):
                counts[read_json(raw)['origin']] += 1
            result = {'schema_version': 1, 'mode': config['mode'], 'config': config,
                      'observations': sum(counts.values()),
                      'local_observations': counts['local'], 'external_observations': counts['external'],
                      'decisions': db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],
                      'sources': self.store.all(db, 'sources')}
        result['budget'] = self._budget('status')
        result['recovery'] = self.recovery_status()
        return result

    def export(self):
        """Bounded convenience API; CLI uses export_to for arbitrary-size state."""
        class BoundedBuffer(io.StringIO):
            def write(self, value):
                if self.tell() + len(value) > 64 * 1024 * 1024:
                    raise RoutingError('In-memory export exceeds 64 MiB; use the streaming routing export CLI.', 78)
                return super().write(value)
        stream = BoundedBuffer()
        self.export_to(stream)
        import json
        return json.loads(stream.getvalue())

    def export_to(self, stream):
        """Stream a coherent JSON snapshot without materializing large tables."""
        from . import routing_budget, routing_recovery
        with self._transaction() as db:
            config = self._config(db)
            routing_budget.initialize(db)
            routing_recovery.initialize(db)
            header = {'schema_version': 1, 'format': 'deepseek-team-routing-export', 'config': config,
                      'budget': routing_budget.status(db, config), 'recovery': routing_recovery.status(db, config)}
            stream.write(canonical(header)[:-1])
            for table in ('observations', 'sources', 'decisions'):
                stream.write(',"' + table + '":[')
                first = True
                for (raw,) in db.execute(f'SELECT value FROM {table} ORDER BY id'):
                    stream.write(('' if first else ',') + canonical(read_json(raw)))
                    first = False
                stream.write(']')
            stream.write(',"audit":[')
            first = True
            for row in db.execute('SELECT * FROM audit ORDER BY sequence'):
                value = dict(zip(('sequence', 'at', 'kind', 'entity', 'digest'), row))
                stream.write(('' if first else ',') + canonical(value))
                first = False
            stream.write(']}\n')
