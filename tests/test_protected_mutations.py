"""Focused checks for protected coordinator write authority.

A protected coordinator deliverable declares a context/read scope, so it never
grants blanket source authority. A source mutation is authorized only by an
exact decision artifact contained in that scope, or by a validated integration
write scope whose exact files are backed by referenced terminal worker changes
or a currently accepted ordinary coordinator write. Ordinary deliverables and
genuine small tasks keep their normal scope authority, and every pending-worker
gate keeps precedence.
"""
import os

from codex_deepseek_team import coordination, coordinator_activity, coordinator_hooks, project, settings
from test_coordination import CoordinationCase

PROTECTED_KINDS = ('security', 'architecture', 'final_verification', 'commit_push',
                   'production', 'secret_signing')


def deliverable(identity, kind, scope, executor='coordinator', **extra):
    item = {
        'id': identity, 'kind': kind, 'scope': list(scope), 'executor': executor,
        'acceptance': ['checked'], 'dependencies': [], 'checks': [],
    }
    item.update(extra)
    return item


class ProtectedCase(CoordinationCase):
    def setUp(self):
        super().setUp()
        settings.set_values(self.repo / settings.PROJECT_FILE,
                            delegation_level=25, access='full-access')
        project.attach(self.repo, coordinator='both')
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'core.py').write_text('VALUE = 1\n')
        (self.repo / 'src' / 'integrated.py').write_text('COMPOSED = 1\n')
        (self.repo / 'docs').mkdir()
        self._counter = 0

    def hook(self, runtime, session, event, **extra):
        payload = {'session_id': session, 'turn_id': 'turn-1', 'cwd': str(self.repo),
                   'hook_event_name': event, 'permission_mode': 'default'}
        payload.update(extra)
        return coordinator_hooks.handle(payload, runtime=runtime)

    def submit(self, deliverables, *, classification='substantial', runtime='codex'):
        self._counter += 1
        session = f'protected-{self._counter}'
        self.hook(runtime, session, 'UserPromptSubmit', prompt='Implement the feature')
        task = coordination.latest_task(self.repo, session)
        plan = {'classification': classification, 'deliverables': deliverables}
        if classification == 'small':
            plan['small_evidence'] = 'One concrete file changes.'
        return session, coordination.plan_task(self.repo, task['id'], plan)

    def write(self, session, path, runtime='codex'):
        return self.hook(runtime, session, 'PreToolUse', tool_name='Write', tool_use_id='w-1',
                         tool_input={'file_path': str(self.repo / path), 'content': 'X = 2\n'})

    def finish_worker(self, task, changes, status='succeeded'):
        assignment = task['assignments'][0]
        coordination.assignment_started(self.repo, task['id'], assignment['id'], 'ws', 'codex', [])
        coordination.assignment_finished(self.repo, task['id'], assignment['id'], status,
                                         'worker output', list(changes), [])

    @staticmethod
    def decision(result):
        return result.get('hookSpecificOutput', {}).get('permissionDecision')

    @staticmethod
    def reason(result):
        return result.get('hookSpecificOutput', {}).get('permissionDecisionReason', '')


class ProtectedScopeTests(ProtectedCase):
    def test_bare_protected_scope_grants_no_write_authority(self):
        for kind in PROTECTED_KINDS:
            with self.subTest(kind=kind):
                session, _ = self.submit([deliverable('guard', kind, ['src'])])
                result = self.write(session, 'src/core.py')
                self.assertEqual(self.decision(result), 'deny')
                self.assertIn('protected-scope gate', self.reason(result))

    def test_unscoped_protected_mutation_is_denied(self):
        session, _ = self.submit([deliverable('guard', 'security', ['docs'],
                                              decision_artifacts=['docs/decision.md'])])
        result = self.hook('codex', session, 'PreToolUse', tool_name='Bash', tool_use_id='u-1',
                           tool_input={'command': "sed -i 's/x/y/' docs/decision.md"})
        self.assertEqual(self.decision(result), 'deny')
        self.assertIn('protected-scope gate', self.reason(result))

    def test_decision_artifact_authorizes_only_the_declared_file(self):
        session, _ = self.submit([deliverable('guard', 'security', ['docs'],
                                              decision_artifacts=['docs/decision.md'])])
        self.assertEqual(self.write(session, 'docs/decision.md'), {})
        denied = self.write(session, 'docs/other.md')
        self.assertEqual(self.decision(denied), 'deny')
        self.assertIn('protected-scope gate', self.reason(denied))

    def test_artifact_directory_glob_and_escape_are_rejected(self):
        cases = ['docs', 'docs/*.md', '../outside.md', str(self.repo / 'docs' / 'decision.md')]
        for index, artifact in enumerate(cases):
            with self.subTest(artifact=artifact):
                if artifact != 'docs':
                    with self.assertRaises(coordination.CoordinationError):
                        self.submit([deliverable('guard', 'security', ['docs'],
                                                 decision_artifacts=[artifact])])
                    continue
                session, _ = self.submit([deliverable('guard', 'security', ['docs'],
                                                      decision_artifacts=[artifact])])
                result = self.write(session, 'docs/decision.md')
                self.assertEqual(self.decision(result), 'deny')
                self.assertIn('decision artifact', self.reason(result))

    def test_artifact_symlink_alias_into_source_is_rejected(self):
        (self.repo / 'docs' / 'alias.md').symlink_to(self.repo / 'src' / 'core.py')
        session, _ = self.submit([deliverable('guard', 'security', ['docs'],
                                              decision_artifacts=['docs/alias.md'])])
        result = self.write(session, 'docs/alias.md')
        self.assertEqual(self.decision(result), 'deny')
        self.assertIn('decision artifact', self.reason(result))

    def test_artifact_hardlink_alias_into_source_is_rejected(self):
        os.link(self.repo / 'src/core.py', self.repo / 'docs/alias.md')
        session, _ = self.submit([deliverable('guard', 'security', ['docs'],
                                              decision_artifacts=['docs/alias.md'])])
        result = self.write(session, 'docs/alias.md')
        self.assertEqual(self.decision(result), 'deny')
        self.assertIsNone(coordinator_activity.exact_file(self.repo, 'docs/alias.md'))

    def test_integration_authorizes_only_its_exact_write_scope(self):
        session, task = self.submit([
            deliverable('impl', 'implementation', ['src/core.py'], executor='worker'),
            deliverable('integ', 'integration', ['docs'], integration_of=['impl'],
                        write_scope=['src/core.py']),
        ])
        self.finish_worker(task, ['src/core.py'])
        allowed, _ = coordinator_activity.mutation_authorizers(
            self.repo, coordination.load_task(self.repo, task['id']), 'src/core.py',
            protected_kinds=coordination.PROTECTED_COORDINATOR_KINDS,
            worker_write_kinds=coordination.WORKER_WRITE_KINDS)
        self.assertIn('integ', [item['id'] for item in allowed])
        self.assertEqual(self.write(session, 'src/core.py'), {})
        denied = self.write(session, 'src/extra.py')
        self.assertEqual(self.decision(denied), 'deny')
        self.assertIn('unplanned', self.reason(denied).lower())

    def test_integration_cannot_expand_a_real_result_to_unrelated_source(self):
        session, task = self.submit([
            deliverable('impl', 'implementation', ['src/core.py'], executor='worker'),
            deliverable('integ', 'integration', ['src'], integration_of=['impl'],
                        write_scope=['src/integrated.py']),
        ])
        self.finish_worker(task, ['src/core.py'])
        result = self.write(session, 'src/integrated.py')
        self.assertEqual(self.decision(result), 'deny')
        self.assertIn('actual', self.reason(result))

    def test_qualified_mutations_cannot_bypass_protected_authority(self):
        session, _ = self.submit([deliverable('guard', 'architecture', ['src'])])
        for tool, data in (
            ('functions.Write', {'file_path': 'src/core.py'}),
            ('tools/Edit', {'file_path': 'src/core.py'}),
            ('functions.NotebookEdit', {'notebook_path': 'src/core.py'}),
            ('functions.apply_patch', {'input': '*** Begin Patch\n*** Update File: src/core.py\n@@\n-X\n+Y\n*** End Patch'}),
        ):
            with self.subTest(tool=tool):
                result = self.hook('codex', session, 'PreToolUse', tool_name=tool, tool_input=data)
                self.assertEqual(self.decision(result), 'deny')

    def test_malformed_persisted_metadata_denies_without_crashing(self):
        for field in ('write_scope', 'integration_of', 'decision_artifacts'):
            for value in (42, {'unexpected': True}, [None], ['\x00bad.md']):
                with self.subTest(field=field, value=value):
                    item = deliverable('integ', 'integration', ['src'])
                    item[field] = value
                    allowed, problem = coordinator_activity.mutation_authorizers(
                        self.repo, {'deliverables': [item]}, 'src/core.py',
                        protected_kinds=coordination.PROTECTED_COORDINATOR_KINDS,
                        worker_write_kinds=coordination.WORKER_WRITE_KINDS)
                    self.assertEqual(allowed, [])
                    self.assertTrue(problem)

    def test_file_integration_never_authorizes_parent_directory_deletion(self):
        session, task = self.submit([
            deliverable('impl', 'implementation', ['src/core.py'], executor='worker'),
            deliverable('integ', 'integration', ['src'], integration_of=['impl'],
                        write_scope=['src/core.py']),
        ])
        self.finish_worker(task, ['src/core.py'], status='failed')
        self.assertEqual(self.write(session, 'src/core.py'), {})
        result = self.hook('codex', session, 'PreToolUse', tool_name='Bash',
                           tool_input={'command': 'rm -rf src'})
        self.assertEqual(self.decision(result), 'deny')

    def test_directory_event_does_not_claim_concrete_coordinator_output(self):
        _, task = self.submit([
            deliverable('docs', 'documentation', ['docs']),
            deliverable('integ', 'integration', ['docs'], integration_of=['docs'],
                        write_scope=['docs/design.md']),
        ])
        coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested', ['docs'])
        coordination.observe_coordinator_result(self.repo, task['id'], 'docs', 'accepted', 'checked')
        current = coordination.load_task(self.repo, task['id'])
        files, issues = coordinator_activity._integration_files(
            self.repo, current, current['deliverables'][1], coordination.WORKER_WRITE_KINDS)
        self.assertEqual(files, [])
        self.assertIn('concrete mutation', issues[0])

    def test_integration_rejects_directory_and_glob_write_scope(self):
        for index, entry in enumerate(('src', 'src/*.py')):
            with self.subTest(entry=entry):
                if '*' in entry:
                    with self.assertRaises(coordination.CoordinationError):
                        self.submit([deliverable('integ', 'integration', ['src'],
                                                 integration_of=['impl'], write_scope=[entry])])
                    continue
                session, task = self.submit([
                    deliverable('impl', 'implementation', ['src/core.py'], executor='worker'),
                    deliverable('integ', 'integration', ['docs'], integration_of=['impl'],
                                write_scope=[entry]),
                ])
                self.finish_worker(task, ['src/core.py'])
                result = self.write(session, 'src/integrated.py')
                self.assertEqual(self.decision(result), 'deny')
                self.assertIn('write_scope', self.reason(result))

    def test_integration_rejects_unknown_self_pending_nonwrite_and_nooutput_refs(self):
        cases = [
            ('unknown', ['impl', 'ghost'], {'id': 'integ'}, 'unknown deliverable'),
            ('self', ['integ'], {'id': 'integ'}, 'cannot reference itself'),
        ]
        for label, refs, extra, expected in cases:
            with self.subTest(label=label):
                integration = deliverable('integ', 'integration', ['docs'], **extra)
                integration['integration_of'] = refs
                integration['write_scope'] = ['src/integrated.py']
                session, task = self.submit([
                    deliverable('impl', 'implementation', ['src/core.py'], executor='worker'),
                    integration,
                ])
                self.finish_worker(task, ['src/core.py'])
                result = self.write(session, 'src/integrated.py')
                self.assertEqual(self.decision(result), 'deny')
                self.assertIn(expected, self.reason(result))

        with self.subTest(label='pending'):
            session, task = self.submit([
                deliverable('impl', 'implementation', ['src/core.py'], executor='worker'),
                deliverable('integ', 'integration', ['docs'], integration_of=['impl'],
                            write_scope=['src/integrated.py']),
            ])
            result = self.write(session, 'src/integrated.py')
            self.assertEqual(self.decision(result), 'deny')
            self.assertIn('pending', self.reason(result))

        with self.subTest(label='non-write'):
            session, task = self.submit([
                deliverable('impl', 'review', ['src/core.py'], executor='worker'),
                deliverable('integ', 'integration', ['docs'], integration_of=['impl'],
                            write_scope=['src/integrated.py']),
            ])
            self.finish_worker(task, ['src/core.py'])
            result = self.write(session, 'src/integrated.py')
            self.assertEqual(self.decision(result), 'deny')
            self.assertIn('not a write deliverable', self.reason(result))

        with self.subTest(label='no-output'):
            session, task = self.submit([
                deliverable('impl', 'implementation', ['src/core.py'], executor='worker'),
                deliverable('integ', 'integration', ['docs'], integration_of=['impl'],
                            write_scope=['src/integrated.py']),
            ])
            self.finish_worker(task, [])
            result = self.write(session, 'src/integrated.py')
            self.assertEqual(self.decision(result), 'deny')
            self.assertIn('worker_changes', self.reason(result))

    def test_integration_accepts_current_accepted_coordinator_write(self):
        session, task = self.submit([
            deliverable('design', 'documentation', ['docs/design.md']),
            deliverable('integ', 'integration', ['docs'], integration_of=['design'],
                        write_scope=['docs/design.md']),
        ])
        coordination.observe_coordinator_result(self.repo, task['id'], 'design', 'accepted',
                                               'design written and reviewed')
        def authorization():
            current = coordination.load_task(self.repo, task['id'])
            return coordinator_activity._integration_files(
                self.repo, current, current['deliverables'][1], coordination.WORKER_WRITE_KINDS)
        self.assertIn('recorded concrete mutation', authorization()[1][0])
        coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested',
                                              ['docs/design.md'])
        coordination.observe_coordinator_result(self.repo, task['id'], 'design', 'accepted',
                                               'design refreshed after the recorded edit')
        self.assertEqual(authorization(), (['docs/design.md'], []))

    def test_integration_rejects_stale_accepted_coordinator_ref(self):
        session, task = self.submit([
            deliverable('design', 'documentation', ['docs/design.md']),
            deliverable('integ', 'integration', ['docs'], integration_of=['design'],
                        write_scope=['docs/design.md']),
        ])
        coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested',
                                              ['docs/design.md'])
        coordination.observe_coordinator_result(self.repo, task['id'], 'design', 'accepted',
                                               'design written')
        def authorization():
            current = coordination.load_task(self.repo, task['id'])
            return coordinator_activity._integration_files(
                self.repo, current, current['deliverables'][1], coordination.WORKER_WRITE_KINDS)
        self.assertEqual(authorization(), (['docs/design.md'], []))
        coordination.record_coordinator_event(self.repo, task['id'], 'mutation_requested',
                                              ['docs/design.md'])
        self.assertEqual(authorization()[0], [])
        self.assertIn('stale', authorization()[1][0])

    def test_pending_worker_precedence_beats_protected_authority(self):
        session, _ = self.submit([
            deliverable('guard', 'security', ['docs'], decision_artifacts=['docs/decision.md']),
            deliverable('impl', 'implementation', ['docs/decision.md'], executor='worker'),
        ])
        result = self.write(session, 'docs/decision.md')
        self.assertEqual(self.decision(result), 'deny')
        self.assertIn('pending worker', self.reason(result))

    def test_ordinary_coordinator_and_small_tasks_keep_scope_authority(self):
        session, _ = self.submit([deliverable('impl', 'implementation', ['src/core.py'])])
        self.assertEqual(self.write(session, 'src/core.py'), {})

        session, _ = self.submit([deliverable('impl', 'implementation', ['src/core.py'])],
                                 classification='small')
        self.assertEqual(self.write(session, 'src/core.py'), {})


if __name__ == '__main__':
    import unittest
    unittest.main()
