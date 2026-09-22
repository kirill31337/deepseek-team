"""Strict, pinned ingestion of public routing-evidence snapshots."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import math
import queue
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from . import routing_models as models


_SHA256 = re.compile(r'[0-9a-f]{64}')
_NATIVE_FIELDS = frozenset(('schema_version', 'format', 'source_family', 'observations'))
_SWE_REQUIRED = frozenset(('schema_version', 'format', 'source_family', 'run_id',
                           'observed_at', 'features', 'evaluated_ids', 'resolved_ids'))
_SWE_OPTIONAL = frozenset(('instance_features', 'costs_usd'))
_IMPORT_FIELDS = frozenset(('origin', 'source_id', 'reliability', 'decision_id'))
_FETCH_TIMEOUT_LIMIT = 300.0


def _https_target(url: str) -> tuple[str, int, str]:
    if (not isinstance(url, str) or not url or len(url) > 2048 or '?' in url or '#' in url or
            any(ord(character) < 32 or ord(character) == 127 for character in url)):
        raise models.RoutingError('Source URL must be a valid HTTPS URL.')
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port or 443
        username, password = parsed.username, parsed.password
    except ValueError:
        raise models.RoutingError('Source URL must be a valid HTTPS URL.') from None
    if (parsed.scheme != 'https' or not hostname or username is not None or password is not None or
            parsed.query or parsed.fragment or not 1 <= port <= 65535):
        raise models.RoutingError(
            'Source URL must use HTTPS without credentials, a query or a fragment.')
    try:
        hostname = hostname.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise models.RoutingError('Source URL contains an invalid hostname.') from None
    if (len(hostname) > 253 or hostname == 'localhost' or hostname.endswith('.localhost') or
            hostname.endswith('.local')):
        raise models.RoutingError('Local source destinations are not permitted.')
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise models.RoutingError('Local or non-public source destinations are not permitted.')
    target = parsed.path or '/'
    if not target.startswith('/'):
        raise models.RoutingError('Source URL contains an invalid path.')
    return hostname, port, target


def _metadata(source_id: str, source_url: str, digest: str, imported_at: float,
              source_format: str, source_family: str) -> dict:
    return {
        'id': source_id,
        'url': source_url,
        'sha256': digest,
        'imported_at': imported_at,
        'format': source_format,
        'source_family': source_family,
    }


def _envelope(value, allowed, required=None):
    required = allowed if required is None else required
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise models.RoutingError('Evidence snapshot has missing or unsupported fields.')


def _observations(value) -> list:
    if not isinstance(value, list) or len(value) > models.MAX_OBSERVATIONS:
        raise models.RoutingError('Evidence snapshot has an invalid observation list.')
    return value


def _native(payload: dict, *, source_id: str, reliability: float, now: float) -> tuple[str, list[dict]]:
    _envelope(payload, _NATIVE_FIELDS)
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1 or \
            payload['format'] != 'deepseek-team-evidence':
        raise models.RoutingError('Unsupported native evidence snapshot version or format.')
    source_family = models.identifier(payload['source_family'], 'source_family')
    result = []
    observation_ids = set()
    for raw in _observations(payload['observations']):
        if not isinstance(raw, dict) or set(raw) & _IMPORT_FIELDS:
            raise models.RoutingError('Native evidence may not set import-controlled fields.')
        if raw.get('source_family') != source_family:
            raise models.RoutingError('Observation source family does not match its snapshot.')
        row = models.validate_observation(
            dict(raw, origin='external', source_id=source_id, reliability=reliability), now=now)
        if row['id'] in observation_ids:
            raise models.RoutingError('Evidence snapshot contains duplicate observation IDs.')
        observation_ids.add(row['id'])
        result.append(row)
    return source_family, result


def _identifier_list(value, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > models.MAX_OBSERVATIONS:
        raise models.RoutingError(f'{name} must be a bounded list of unique identifiers.')
    result = [models.identifier(item, name[:-1]) for item in value]
    if len(result) != len(set(result)):
        raise models.RoutingError(f'{name} must not contain duplicates.')
    return result


def _hashed_id(*parts: str) -> str:
    framed = b''.join(len(part.encode()).to_bytes(4, 'big') + part.encode() for part in parts)
    return hashlib.sha256(framed).hexdigest()


def _swe_bench(payload: dict, *, source_id: str, reliability: float,
               now: float) -> tuple[str, list[dict]]:
    _envelope(payload, _SWE_REQUIRED | _SWE_OPTIONAL, _SWE_REQUIRED)
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1 or \
            payload['format'] != 'swe-bench':
        raise models.RoutingError('Unsupported SWE-bench snapshot version or format.')
    source_family = models.identifier(payload['source_family'], 'source_family')
    run_id = models.identifier(payload['run_id'], 'run_id')
    evaluated = _identifier_list(payload['evaluated_ids'], 'evaluated_ids')
    resolved = _identifier_list(payload['resolved_ids'], 'resolved_ids')
    evaluated_set, resolved_set = set(evaluated), set(resolved)
    if not resolved_set <= evaluated_set:
        raise models.RoutingError('Resolved SWE-bench instances must have been evaluated.')
    common_features = models.validate_features(payload['features'])
    instance_features = payload.get('instance_features', {})
    costs = payload.get('costs_usd', {})
    if (not isinstance(instance_features, dict) or not isinstance(costs, dict) or
            not set(instance_features) <= evaluated_set or not set(costs) <= evaluated_set):
        raise models.RoutingError('SWE-bench instance metadata must match evaluated instances.')

    result = []
    for instance_id in evaluated:
        overrides = instance_features.get(instance_id, {})
        if not isinstance(overrides, dict):
            raise models.RoutingError('SWE-bench instance features must be objects.')
        features = models.validate_features(dict(common_features, **overrides))
        case_id = _hashed_id(source_family, instance_id)
        observation_id = _hashed_id(source_id, run_id, case_id)
        cost = costs.get(instance_id)
        if cost is not None:
            cost = models.number(cost, 'cost_usd')
        result.append(models.validate_observation({
            'id': observation_id,
            'case_id': case_id,
            'origin': 'external',
            'source_id': source_id,
            'source_family': source_family,
            'features': features,
            'action': 'worker',
            'outcome': 'accepted' if instance_id in resolved_set else 'rejected',
            'observed_at': payload['observed_at'],
            'cost_usd': cost,
            'reliability': reliability,
        }, now=now))
    return source_family, result


def parse_snapshot(data: bytes, *, source_id: str, source_url: str,
                   expected_sha256: str, reliability: float = 0.5, now=None) -> dict:
    """Authenticate and normalize one closed evidence snapshot."""
    if not isinstance(data, bytes):
        raise models.RoutingError('Evidence snapshot must be supplied as bytes.')
    source_id = models.identifier(source_id, 'source_id')
    _https_target(source_url)
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise models.RoutingError('Expected SHA-256 must be 64 lowercase hexadecimal characters.')
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(digest, expected_sha256):
        raise models.RoutingError('Evidence snapshot does not match its expected SHA-256.')
    imported_at = models.number(time.time() if now is None else now, 'imported_at')
    reliability = models.number(reliability, 'reliability', 0, 1)
    payload = models.read_json(data)
    if not isinstance(payload, dict):
        raise models.RoutingError('Evidence snapshot must be a JSON object.')
    if payload.get('format') == 'deepseek-team-evidence':
        source_family, observations = _native(
            payload, source_id=source_id, reliability=reliability, now=imported_at)
    elif payload.get('format') == 'swe-bench':
        source_family, observations = _swe_bench(
            payload, source_id=source_id, reliability=reliability, now=imported_at)
    else:
        raise models.RoutingError('Unsupported evidence snapshot format.')
    return {
        'metadata': _metadata(source_id, source_url, digest, imported_at,
                              payload['format'], source_family),
        'observations': observations,
    }


def _resolve_public_addresses(host: str, port: int) -> list[tuple]:
    try:
        answers = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM,
                                     socket.IPPROTO_TCP)
    except OSError:
        raise models.RoutingError('Unable to resolve source host.') from None
    addresses = []
    seen = set()
    for family, socktype, protocol, _canonical_name, sockaddr in answers:
        if (family not in (socket.AF_INET, socket.AF_INET6) or
                socktype != socket.SOCK_STREAM or protocol not in (0, socket.IPPROTO_TCP) or
                not isinstance(sockaddr, tuple) or len(sockaddr) < 2 or sockaddr[1] != port):
            raise models.RoutingError('Source host resolved to an unsupported destination.')
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            raise models.RoutingError('Source host resolved to an invalid destination.') from None
        if not address.is_global:
            raise models.RoutingError('Source host resolved to a local or non-public destination.')
        pinned = (family, socktype, protocol, sockaddr)
        key = (family, sockaddr)
        if key not in seen:
            seen.add(key)
            addresses.append(pinned)
    if not addresses:
        raise models.RoutingError('Source host did not resolve to a usable destination.')
    return addresses


def _resolve_with_timeout(host: str, port: int, timeout: float) -> list[tuple]:
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((None, _resolve_public_addresses(host, port)))
        except Exception as error:
            result.put((error, None))

    threading.Thread(target=resolve, daemon=True).start()
    try:
        error, addresses = result.get(timeout=timeout)
    except queue.Empty:
        raise models.RoutingError('Source fetch timed out during DNS resolution.') from None
    if error is not None:
        raise error
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, addresses: list[tuple], *, timeout: float,
                 context=None):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_addresses = addresses

    def connect(self):
        last_error = None
        for family, socktype, protocol, sockaddr in self._pinned_addresses:
            raw = socket.socket(family, socktype, protocol)
            try:
                raw.settimeout(self.timeout)
                if self.source_address:
                    raw.bind(self.source_address)
                raw.connect(sockaddr)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError as error:
                last_error = error
                raw.close()
        if last_error is None:
            raise OSError('no pinned address')
        raise last_error


def _fetch_limits(max_bytes, timeout) -> tuple[int, float]:
    if type(max_bytes) is not int or not 0 < max_bytes <= models.MAX_JSON_BYTES:
        raise models.RoutingError('Fetch size limit must be between 1 byte and 8 MiB.')
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or
            not 0 < timeout <= _FETCH_TIMEOUT_LIMIT):
        raise models.RoutingError('Fetch timeout must be a finite positive number at most 300 seconds.')
    return max_bytes, float(timeout)


def fetch_snapshot(url: str, *, max_bytes: int = models.MAX_JSON_BYTES,
                   timeout: float = 20) -> bytes:
    """Fetch bytes directly from a validated, DNS-pinned HTTPS destination."""
    max_bytes, timeout = _fetch_limits(max_bytes, timeout)
    host, port, target = _https_target(url)
    started = time.monotonic()
    addresses = _resolve_with_timeout(host, port, timeout)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise models.RoutingError('Source fetch timed out.')
    connection = _PinnedHTTPSConnection(host, port, addresses, timeout=remaining)
    try:
        connection.request('GET', target, headers={
            'Accept': 'application/json',
            'Accept-Encoding': 'identity',
            'Connection': 'close',
            'User-Agent': 'deepseek-team/1',
        })
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise models.RoutingError('Source redirects are not permitted.')
        if response.status != 200:
            raise models.RoutingError('Source returned an unsuccessful HTTP status.')
        content_length = response.getheader('Content-Length')
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                raise models.RoutingError('Source returned an invalid content length.') from None
            if declared_length < 0 or declared_length > max_bytes:
                raise models.RoutingError('Source response exceeds the configured size limit.')
        else:
            declared_length = None

        body = bytearray()
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise models.RoutingError('Source fetch timed out.')
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_bytes + 1 - len(body)))
            if not isinstance(chunk, bytes):
                raise models.RoutingError('Source returned an invalid response body.')
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_bytes:
                raise models.RoutingError('Source response exceeds the configured size limit.')
        if declared_length is not None and len(body) != declared_length:
            raise models.RoutingError('Source response was truncated.')
        return bytes(body)
    except models.RoutingError:
        raise
    except (OSError, http.client.HTTPException):
        raise models.RoutingError('Unable to fetch source snapshot securely.') from None
    finally:
        connection.close()
