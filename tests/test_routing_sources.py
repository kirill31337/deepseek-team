import copy
import hashlib
import json
import socket
import threading
import time
import unittest
from unittest import mock

from codex_deepseek_team import routing_models as models
from codex_deepseek_team import routing_sources as sources


def encoded(value):
    return json.dumps(value, separators=(',', ':')).encode()


def native_snapshot():
    return {
        'schema_version': 1,
        'format': 'deepseek-team-evidence',
        'source_family': 'synthetic-native',
        'observations': [{
            'id': 'native.obs-1',
            'case_id': 'native.case-1',
            'source_family': 'synthetic-native',
            'features': {'kind': 'test', 'domain': 'python'},
            'action': 'worker',
            'outcome': 'accepted',
            'observed_at': 100,
            'cost_usd': 1.25,
        }],
    }


def swe_snapshot():
    return {
        'schema_version': 1,
        'format': 'swe-bench',
        'source_family': 'synthetic-swe',
        'run_id': 'run-2026-09-22',
        'observed_at': 100,
        'features': {
            'kind': 'implementation',
            'domain': 'python',
            'runtime': 'codex',
            'model': 'deepseek-v3',
            'effort': 'high',
        },
        'evaluated_ids': ['django__django-1', 'flask__flask-2'],
        'resolved_ids': ['django__django-1'],
        'instance_features': {'flask__flask-2': {'scope_size': 'large'}},
        'costs_usd': {'django__django-1': 1.5},
    }


def parse(value, **changes):
    data = encoded(value)
    arguments = {
        'source_id': 'fixture.source',
        'source_url': 'https://evidence.example/snapshot.json',
        'expected_sha256': hashlib.sha256(data).hexdigest(),
        'reliability': 0.75,
        'now': 200,
    }
    arguments.update(changes)
    return sources.parse_snapshot(data, **arguments)


class SnapshotParsingTests(unittest.TestCase):
    def test_native_snapshot_is_pinned_normalized_and_attributed(self):
        result = parse(native_snapshot())

        self.assertEqual(result['metadata'], {
            'id': 'fixture.source',
            'url': 'https://evidence.example/snapshot.json',
            'sha256': hashlib.sha256(encoded(native_snapshot())).hexdigest(),
            'imported_at': 200.0,
            'format': 'deepseek-team-evidence',
            'source_family': 'synthetic-native',
        })
        observation = result['observations'][0]
        self.assertEqual(observation['origin'], 'external')
        self.assertEqual(observation['source_id'], 'fixture.source')
        self.assertEqual(observation['source_family'], 'synthetic-native')
        self.assertEqual(observation['reliability'], 0.75)
        self.assertEqual(observation['features']['kind'], 'test')
        self.assertIsNone(observation['duration_seconds'])

    def test_snapshot_hash_must_be_lowercase_sha256_and_match_bytes(self):
        payload = native_snapshot()
        digest = hashlib.sha256(encoded(payload)).hexdigest()
        for expected in ('0' * 64, digest.upper(), 'sha256:' + digest, 'short'):
            with self.subTest(expected=expected), self.assertRaises(models.RoutingError):
                parse(payload, expected_sha256=expected)

    def test_native_envelope_and_observations_are_closed(self):
        cases = []
        with_unknown_envelope = native_snapshot()
        with_unknown_envelope['notes'] = 'not admissible evidence'
        cases.append(with_unknown_envelope)
        boolean_version = native_snapshot()
        boolean_version['schema_version'] = True
        cases.append(boolean_version)
        with_prompt = native_snapshot()
        with_prompt['observations'][0]['prompt'] = 'private material'
        cases.append(with_prompt)
        wrong_family = native_snapshot()
        wrong_family['observations'][0]['source_family'] = 'other-family'
        cases.append(wrong_family)
        for controlled in ('origin', 'source_id', 'reliability'):
            value = native_snapshot()
            value['observations'][0][controlled] = 'external' if controlled == 'origin' else 'supplied'
            cases.append(value)
        for decision_id in ('private-decision', None):
            value = native_snapshot()
            value['observations'][0]['decision_id'] = decision_id
            cases.append(value)

        for value in cases:
            with self.subTest(value=value), self.assertRaises(models.RoutingError):
                parse(value)

    def test_native_duplicate_observation_ids_are_rejected(self):
        value = native_snapshot()
        value['observations'].append(copy.deepcopy(value['observations'][0]))
        value['observations'][1]['case_id'] = 'native.case-2'

        with self.assertRaises(models.RoutingError):
            parse(value)

    def test_duplicate_json_fields_and_malformed_json_are_rejected(self):
        duplicate = (b'{"schema_version":1,"schema_version":1,'
                     b'"format":"deepseek-team-evidence",'
                     b'"source_family":"synthetic","observations":[]}')
        arguments = dict(source_id='fixture.source', source_url='https://evidence.example/x',
                         reliability=.5, now=200)
        for data in (duplicate, b'{broken', b'{"x":NaN}'):
            arguments['expected_sha256'] = hashlib.sha256(data).hexdigest()
            with self.subTest(data=data), self.assertRaises(models.RoutingError):
                sources.parse_snapshot(data, **arguments)

    def test_snapshot_metadata_and_provenance_are_validated_even_when_empty(self):
        payload = native_snapshot()
        payload['observations'] = []
        cases = (
            {'source_id': '../unsafe'},
            {'source_url': 'http://evidence.example/x'},
            {'source_url': 'https://user:secret@evidence.example/x'},
            {'source_url': 'https://evidence.example/x?token=secret'},
            {'source_url': 'https://evidence.example/x#part'},
            {'reliability': True},
            {'reliability': 1.1},
            {'now': -1},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(models.RoutingError):
                parse(payload, **changes)

    def test_swe_bench_resolved_and_unresolved_become_labelled_observations(self):
        result = parse(swe_snapshot())
        by_case = {row['features']['scope_size']: row for row in result['observations']}

        self.assertEqual(result['metadata']['format'], 'swe-bench')
        self.assertEqual(len(result['observations']), 2)
        self.assertEqual({row['outcome'] for row in result['observations']},
                         {'accepted', 'rejected'})
        self.assertEqual(by_case['large']['outcome'], 'rejected')
        accepted = next(row for row in result['observations'] if row['outcome'] == 'accepted')
        self.assertEqual(accepted['cost_usd'], 1.5)
        self.assertEqual(accepted['action'], 'worker')
        self.assertEqual(accepted['origin'], 'external')
        self.assertNotEqual(result['observations'][0]['id'], result['observations'][1]['id'])

    def test_swe_case_identity_survives_runs_while_observation_identity_does_not(self):
        first = parse(swe_snapshot())['observations'][0]
        later = swe_snapshot()
        later['run_id'] = 'run-2026-09-23'
        second = parse(later)['observations'][0]

        self.assertEqual(first['case_id'], second['case_id'])
        self.assertNotEqual(first['id'], second['id'])

    def test_swe_bench_rejects_ambiguous_sets_and_unmatched_maps(self):
        cases = []
        duplicate_evaluated = swe_snapshot()
        duplicate_evaluated['evaluated_ids'].append('django__django-1')
        cases.append(duplicate_evaluated)
        duplicate_resolved = swe_snapshot()
        duplicate_resolved['resolved_ids'].append('django__django-1')
        cases.append(duplicate_resolved)
        not_evaluated = swe_snapshot()
        not_evaluated['resolved_ids'].append('numpy__numpy-3')
        cases.append(not_evaluated)
        stray_features = swe_snapshot()
        stray_features['instance_features']['numpy__numpy-3'] = {}
        cases.append(stray_features)
        stray_cost = swe_snapshot()
        stray_cost['costs_usd']['numpy__numpy-3'] = 2
        cases.append(stray_cost)

        for value in cases:
            with self.subTest(value=value), self.assertRaises(models.RoutingError):
                parse(value)

    def test_swe_bench_envelope_is_closed_and_versioned(self):
        cases = []
        for field, value in (('schema_version', 2), ('schema_version', True),
                             ('format', 'swebench')):
            payload = swe_snapshot()
            payload[field] = value
            cases.append(payload)
        unknown = swe_snapshot()
        unknown['error_ids'] = ['flask__flask-2']
        cases.append(unknown)
        bad_cost = swe_snapshot()
        bad_cost['costs_usd']['django__django-1'] = -1
        cases.append(bad_cost)

        for value in cases:
            with self.subTest(value=value), self.assertRaises(models.RoutingError):
                parse(value)


class FakeResponse:
    def __init__(self, chunks, *, status=200, content_length=None):
        self.status = status
        self.reason = 'synthetic response'
        self._chunks = iter(chunks)
        self._content_length = content_length

    def getheader(self, name, default=None):
        if name.lower() == 'content-length' and self._content_length is not None:
            return str(self._content_length)
        return default

    def read1(self, _size):
        return next(self._chunks, b'')


class FakeConnection:
    instances = []
    response = FakeResponse([b'{}'])

    def __init__(self, host, port, addresses, *, timeout):
        self.host = host
        self.port = port
        self.addresses = addresses
        self.timeout = timeout
        self.requests = []
        self.closed = False
        self.sock = mock.Mock()
        self.__class__.instances.append(self)

    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, body, headers))

    def getresponse(self):
        return self.__class__.response

    def close(self):
        self.closed = True


class SnapshotFetchingTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances = []
        FakeConnection.response = FakeResponse([b'{', b'}'])

    @mock.patch.object(sources, '_PinnedHTTPSConnection', FakeConnection)
    @mock.patch.object(sources, '_resolve_public_addresses')
    def test_fetch_uses_direct_pinned_https_and_returns_bounded_body(self, resolve):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                      ('203.0.113.10', 443))]
        resolve.return_value = addresses

        self.assertEqual(sources.fetch_snapshot('https://evidence.example/reports/latest.json',
                                                max_bytes=10, timeout=7), b'{}')
        connection = FakeConnection.instances[0]
        self.assertEqual((connection.host, connection.port, connection.addresses),
                         ('evidence.example', 443, addresses))
        self.assertGreater(connection.timeout, 0)
        self.assertLessEqual(connection.timeout, 7)
        self.assertEqual(connection.requests, [(
            'GET', '/reports/latest.json', None,
            {'Accept': 'application/json', 'Accept-Encoding': 'identity',
             'Connection': 'close', 'User-Agent': 'deepseek-team/1'},
        )])
        self.assertTrue(connection.closed)

    def test_fetch_rejects_non_https_credentials_queries_and_fragments(self):
        urls = (
            'http://evidence.example/x',
            'https://user:secret@evidence.example/x',
            'https://evidence.example/x?credential=secret',
            'https://evidence.example/x?',
            'https://evidence.example/x#fragment',
            'https://evidence.example/x#',
            'https:///missing-host',
        )
        with mock.patch.object(sources.socket, 'getaddrinfo') as lookup:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(models.RoutingError):
                    sources.fetch_snapshot(url)
            lookup.assert_not_called()

    def test_fetch_rejects_every_non_global_dns_destination(self):
        blocked = ('127.0.0.1', '10.2.3.4', '169.254.1.1', '224.0.0.1',
                   '192.0.2.10', '::1', 'fe80::1', 'ff02::1')
        for address in blocked:
            family = socket.AF_INET6 if ':' in address else socket.AF_INET
            sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
            answer = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', sockaddr)]
            with self.subTest(address=address), \
                    mock.patch.object(sources.socket, 'getaddrinfo', return_value=answer), \
                    self.assertRaises(models.RoutingError):
                sources.fetch_snapshot('https://evidence.example/x')

    def test_dns_results_cannot_change_the_validated_destination_port(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                   ('93.184.216.34', 444))]
        with mock.patch.object(sources.socket, 'getaddrinfo', return_value=answer), \
                self.assertRaises(models.RoutingError):
            sources._resolve_public_addresses('evidence.example', 443)

    @mock.patch.object(sources, '_PinnedHTTPSConnection', FakeConnection)
    @mock.patch.object(sources, '_resolve_public_addresses', return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ('203.0.113.10', 443))])
    def test_fetch_rejects_redirects_errors_and_oversized_responses(self, _resolve):
        cases = (
            FakeResponse([], status=302),
            FakeResponse([], status=404),
            FakeResponse([], content_length=11),
            FakeResponse([b'123456', b'78901']),
        )
        for response in cases:
            FakeConnection.response = response
            with self.subTest(status=response.status), self.assertRaises(models.RoutingError):
                sources.fetch_snapshot('https://evidence.example/x', max_bytes=10)
            self.assertTrue(FakeConnection.instances[-1].closed)

    def test_fetch_rejects_invalid_limits_before_dns(self):
        with mock.patch.object(sources.socket, 'getaddrinfo') as lookup:
            for arguments in ({'max_bytes': 0}, {'max_bytes': True}, {'timeout': 0},
                              {'timeout': float('inf')}, {'timeout': True}):
                with self.subTest(arguments=arguments), self.assertRaises(models.RoutingError):
                    sources.fetch_snapshot('https://evidence.example/x', **arguments)
            lookup.assert_not_called()

    def test_fetch_timeout_includes_dns_resolution(self):
        release = threading.Event()

        def blocked_resolution(_host, _port):
            release.wait(.5)
            return []

        started = time.monotonic()
        try:
            with mock.patch.object(sources, '_resolve_public_addresses', blocked_resolution), \
                    self.assertRaises(models.RoutingError):
                sources.fetch_snapshot('https://evidence.example/x', timeout=.01)
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, .2)

    def test_pinned_connection_wraps_validated_address_with_original_tls_hostname(self):
        raw_socket = mock.Mock()
        tls_socket = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = tls_socket
        address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                   ('93.184.216.34', 443))
        connection = sources._PinnedHTTPSConnection(
            'evidence.example', 443, [address], timeout=4, context=context)

        with mock.patch.object(sources.socket, 'socket', return_value=raw_socket):
            connection.connect()

        raw_socket.settimeout.assert_called_once_with(4)
        raw_socket.connect.assert_called_once_with(('93.184.216.34', 443))
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname='evidence.example')
        self.assertIs(connection.sock, tls_socket)


if __name__ == '__main__':
    unittest.main()
