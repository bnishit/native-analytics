import base64
import hashlib
import http.client
import json
import math
import os
import re
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Annotated, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator


_UUID = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
_PROPERTY = r'^[A-Za-z_$][A-Za-z0-9_$]{0,79}$'
_SHA = re.compile(r'[0-9a-f]{40}')
_HOSTS = {'us': 'us.posthog.com', 'eu': 'eu.posthog.com'}
_COLUMNS = ['total', 'success', 'failure', 'unclassified', 'universe_total', 'environment_missing', 'release_missing']
_LIMIT = 128 * 1024
_MAX_COUNT = 2 ** 53 - 1
_RELEASE_QUERY = '''query AfterspanRelease($serviceId: String!, $environmentId: String!) {
  serviceInstance(serviceId: $serviceId, environmentId: $environmentId) {
    latestDeployment { id projectId serviceId environmentId status createdAt statusUpdatedAt meta environment { id name } }
  }
}'''


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)


class GitHubConfig(_Model):
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')

    @field_validator('repository')
    @classmethod
    def named_repository(cls, value):
        if any(part in {'.', '..'} for part in value.split('/')):
            raise ValueError('invalid repository')
        return value


class PostHogConfig(_Model):
    region: Literal['us', 'eu']
    project_id: int = Field(gt=0, le=2 ** 31 - 1)


class RailwayConfig(_Model):
    project_id: str = Field(pattern=_UUID)
    environment_id: str = Field(pattern=_UUID)
    service_id: str = Field(pattern=_UUID)


_CONFIGS = {'github': GitHubConfig, 'posthog': PostHogConfig, 'railway': RailwayConfig}
Scalar = str | int | float | bool
Property = Annotated[str, Field(pattern=_PROPERTY)]


def _scalar(value):
    if (type(value) not in (str, int, float, bool)
            or isinstance(value, str) and (len(value) > 256 or any(ord(c) < 32 for c in value))
            or type(value) in (int, float) and (abs(value) > _MAX_COUNT or not math.isfinite(value))):
        raise ValueError('unsupported scalar')
    return value


class OutcomeFilter(_Model):
    property: Property
    op: Literal['eq', 'in']
    value: Scalar | list[Scalar]

    @model_validator(mode='after')
    def scalar_values(self):
        if self.op == 'in':
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 20:
                raise ValueError('in requires bounded scalar values')
            for value in self.value:
                _scalar(value)
        else:
            _scalar(self.value)
        return self


class EventOutcome(_Model):
    kind: Literal['event-outcome-v1']
    unit: Literal['logged_event']
    event: str = Field(min_length=1, max_length=160, pattern=r'^[^\x00-\x1f]+$')
    filters: list[OutcomeFilter] = Field(max_length=10)
    outcome_property: Property
    success_values: list[Scalar] = Field(min_length=1, max_length=20)
    failure_values: list[Scalar] = Field(min_length=1, max_length=20)
    min_sample_size: int = Field(ge=1, le=10000000)

    @model_validator(mode='after')
    def disjoint_outcomes(self):
        for value in self.success_values + self.failure_values:
            _scalar(value)
        if any(_equal(a, b) for a in self.success_values for b in self.failure_values):
            raise ValueError('outcome values must be disjoint')
        return self


class PostHogMapping(_Model):
    kind: Literal['posthog']
    environment: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_.-]+$')
    environment_property: Property
    release_property: Property | None
    connection_id: str = Field(pattern=_UUID)
    connection_revision: int = Field(ge=1)


class SourceWindow(_Model):
    entry_start: datetime
    entry_end: datetime
    as_of: datetime
    release: str | None = Field(default=None, pattern=r'^[0-9a-f]{40}$')

    @field_validator('entry_start', 'entry_end', 'as_of', mode='before')
    @classmethod
    def utc_timestamp(cls, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('use a timestamp with a timezone')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def bounded_window(self):
        if (not self.entry_start < self.entry_end <= self.as_of
                or (self.entry_end - self.entry_start).total_seconds() > 31 * 86400
                or self.as_of.timestamp() > time.time() + 5):
            raise ValueError('invalid bounded source window')
        return self


def _equal(a, b):
    return a == b and (type(a) is type(b) or type(a) in (int, float) and type(b) in (int, float))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _config(kind, config):
    if not isinstance(kind, str) or kind not in _CONFIGS:
        raise HTTPException(422, 'Unsupported connection kind')
    try:
        return _CONFIGS[kind].model_validate(config).model_dump()
    except ValidationError:
        raise HTTPException(422, 'Invalid connection target') from None


def _secret(secret):
    if not isinstance(secret, SecretStr):
        raise HTTPException(422, 'A private credential is required')
    value = secret.get_secret_value()
    if not value or len(value) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise HTTPException(422, 'Invalid private credential')
    return value


def _cipher():
    try:
        value = base64.b64decode(os.environ.get('AFTERSPAN_CONNECTION_KEY', ''), altchars=b'-_', validate=True)
        if len(value) != 32:
            raise ValueError()
        return AESGCM(value)
    except (ValueError, TypeError):
        raise HTTPException(503, 'Private connection storage is not configured') from None


def _aad(project, id, revision, kind):
    return _json([project, id, revision, kind]).encode()


def _public(row):
    return {key: row[key] for key in ('id', 'revision', 'kind', 'status')} | {'target': json.loads(row['config'])}


class Connections:
    def __init__(self, connect):
        self.connect = connect

    @staticmethod
    def init_db(c):
        c.execute('''CREATE TABLE IF NOT EXISTS loop_provider_connections(
            project TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, revision INTEGER NOT NULL,
            kind TEXT NOT NULL, config TEXT NOT NULL, secret BLOB, status TEXT NOT NULL,
            created REAL NOT NULL, PRIMARY KEY(project,id,revision))''')

    def put(self, project, id, expected_revision, kind, config, secret):
        config = _config(kind, config)
        value, cipher = _secret(secret), _cipher()
        if not isinstance(id, str) or not re.fullmatch(_UUID, id) or type(expected_revision) is not int or expected_revision < 0:
            raise HTTPException(422, 'Invalid connection revision')
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone():
                raise HTTPException(404, 'Project not found')
            row = c.execute('SELECT * FROM loop_provider_connections WHERE project=? AND id=? ORDER BY revision DESC LIMIT 1', (project, id)).fetchone()
            if expected_revision != (row['revision'] if row else 0) or row and row['kind'] != kind:
                raise HTTPException(409, 'Connection revision changed')
            revision, nonce = expected_revision + 1, os.urandom(12)
            encrypted = nonce + cipher.encrypt(nonce, value.encode(), _aad(project, id, revision, kind))
            c.execute('INSERT INTO loop_provider_connections VALUES(?,?,?,?,?,?,?,?)',
                      (project, id, revision, kind, _json(config), encrypted, 'configured', time.time()))
        return {'id': id, 'revision': revision, 'kind': kind, 'status': 'configured', 'target': config}

    def list(self, project):
        with closing(self.connect()) as c:
            rows = c.execute('''SELECT a.* FROM loop_provider_connections a WHERE project=? AND revision=(
                SELECT max(b.revision) FROM loop_provider_connections b WHERE b.project=a.project AND b.id=a.id)
                ORDER BY id''', (project,)).fetchall()
        return [_public(row) for row in rows]

    def resolve(self, project, id, kind=None):
        with closing(self.connect()) as c:
            row = c.execute('SELECT * FROM loop_provider_connections WHERE project=? AND id=? ORDER BY revision DESC LIMIT 1', (project, id)).fetchone()
        if row is None:
            raise HTTPException(404, 'Connection not found')
        if row['status'] != 'configured' or kind is not None and row['kind'] != kind:
            raise HTTPException(409, 'Connection is unavailable')
        cipher = _cipher()
        try:
            value = row['secret']
            secret = cipher.decrypt(value[:12], value[12:], _aad(project, id, row['revision'], row['kind'])).decode()
        except (InvalidTag, ValueError, TypeError, UnicodeError):
            raise HTTPException(503, 'Private connection could not be read') from None
        return {'id': id, 'revision': row['revision'], 'kind': row['kind'],
                'config': json.loads(row['config']), 'secret': SecretStr(secret)}

    def revoke(self, project, id, expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise HTTPException(422, 'Invalid connection revision')
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM loop_provider_connections WHERE project=? AND id=? ORDER BY revision DESC LIMIT 1', (project, id)).fetchone()
            if row is None:
                raise HTTPException(404, 'Connection not found')
            if row['revision'] != expected_revision or row['status'] == 'revoked':
                raise HTTPException(409, 'Connection revision changed')
            c.execute('INSERT INTO loop_provider_connections VALUES(?,?,?,?,?,?,?,?)',
                      (project, id, expected_revision + 1, row['kind'], row['config'], None, 'revoked', time.time()))
        return _public(row) | {'revision': expected_revision + 1, 'status': 'revoked'}


class _Unavailable(Exception):
    pass


def _request(host, method, path, secret, body=None):
    if host not in {*_HOSTS.values(), 'api.github.com', 'backboard.railway.app'}:
        raise HTTPException(422, 'Unsupported provider host')
    headers = {'Authorization': 'Bearer ' + _secret(secret), 'Content-Type': 'application/json',
               'Accept': 'application/json', 'Accept-Encoding': 'identity', 'User-Agent': 'afterspan-loop'}
    c = http.client.HTTPSConnection(host, timeout=10)
    deadline = time.monotonic() + 15
    try:
        c.request(method, path, body=None if body is None else _json(body).encode(), headers=headers)
        response = c.getresponse()
        if response.status != 200:
            raise _Unavailable('provider_denied' if response.status in (401, 403) else 'provider_unavailable')
        raw = bytearray()
        while len(raw) <= _LIMIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if c.sock is not None:
                c.sock.settimeout(min(10, remaining))
            chunk = response.read1(min(16384, _LIMIT + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > _LIMIT:
            raise _Unavailable('response_too_large')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise _Unavailable('malformed_response')
        return data
    except TimeoutError:
        raise _Unavailable('provider_timeout') from None
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        raise _Unavailable('provider_unavailable') from None
    finally:
        c.close()


def _literal(value):
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def _predicate(property, values):
    key = _literal(property)
    clauses = []
    for value in values:
        if type(value) in (int, float):
            clauses.append(f"(JSONType(properties, {key}) IN ('Int64', 'UInt64', 'Float64') AND JSONExtractFloat(properties, {key}) = {value})")
        elif isinstance(value, str):
            clauses.append(f"(JSONType(properties, {key}) = 'String' AND JSONExtractString(properties, {key}) = {_literal(value)})")
        else:
            clauses.append(f'JSONExtractRaw(properties, {key}) = {_literal(_json(value))}')
    return '(' + ' OR '.join(clauses) + ')'


def _missing(property):
    if property is None:
        return '1 = 1'
    key = _literal(property)
    return f"(JSONType(properties, {key}) != 'String' OR JSONExtractString(properties, {key}) = '')"


def _query(measurement, mapping, window):
    scope = [_predicate(mapping.environment_property, [mapping.environment])]
    if window.release:
        if mapping.release_property is None:
            raise HTTPException(422, 'This source has no release mapping')
        scope.append(_predicate(mapping.release_property, [window.release]))
    cohort = ' AND '.join(scope)
    success = _predicate(measurement.outcome_property, measurement.success_values)
    failure = _predicate(measurement.outcome_property, measurement.failure_values)
    aggregates = [f'countIf({cohort})', f'countIf({cohort} AND {success})',
                  f'countIf({cohort} AND {failure})', f'countIf({cohort} AND NOT ({success} OR {failure}))',
                  'count()', f'countIf({_missing(mapping.environment_property)})',
                  f'countIf({_missing(mapping.release_property)})']
    clauses = ['event = ' + _literal(measurement.event)]
    for name, op, value in [('timestamp', '>=', window.entry_start), ('timestamp', '<', window.entry_end), ('created_at', '<=', window.as_of)]:
        clauses.append(f"{name} {op} toDateTime64({_literal(value.strftime('%Y-%m-%d %H:%M:%S.%f'))}, 6, 'UTC')")
    for condition in measurement.filters:
        clauses.append(_predicate(condition.property, condition.value if condition.op == 'in' else [condition.value]))
    return 'SELECT ' + ', '.join(f'{expression} AS {name}' for expression, name in zip(aggregates, _COLUMNS)) + ' FROM events WHERE ' + ' AND '.join(clauses) + ' LIMIT 1'


def _aggregate(data, columns):
    status = data.get('query_status')
    if status is not None and (not isinstance(status, dict) or status.get('complete') is not True):
        raise _Unavailable('partial_response')
    for part in (data, status or {}):
        if any(part.get(k) not in (None, False) for k in ('hasMore', 'has_more', 'truncated', 'partial', 'is_partial',
                'error', 'errors', 'error_code', 'error_message', 'next', 'timed_out', 'timeout', 'cancelled', 'running', 'is_running', 'is_cached')):
            raise _Unavailable('partial_response')
    if data.get('timezone', 'UTC') != 'UTC' or data.get('status', 'complete') not in ('complete', 'completed'):
        raise _Unavailable('partial_response')
    results = data.get('results')
    if (data.get('columns') != columns or not isinstance(results, list) or len(results) != 1
            or not isinstance(results[0], list) or len(results[0]) != len(columns)
            or any(type(value) is not int or not 0 <= value <= _MAX_COUNT for value in results[0])):
        raise _Unavailable('malformed_counts')
    return dict(zip(columns, results[0]))


def posthog_observe(connection, definition, window):
    try:
        measurement = EventOutcome.model_validate(definition['measurement'])
        mapping = PostHogMapping.model_validate(definition['mapping'])
        window = SourceWindow.model_validate(window)
    except (ValidationError, KeyError, TypeError):
        raise HTTPException(422, 'Invalid source measurement or window') from None
    if connection['kind'] != 'posthog' or mapping.connection_id != connection['id'] or mapping.connection_revision != connection['revision']:
        raise HTTPException(409, 'Source connection revision changed')
    config = _config('posthog', connection['config'])
    query = _query(measurement, mapping, window)
    receipt = {'source': 'posthog', 'connection_id': connection['id'], 'connection_revision': connection['revision'],
               'provider_project': config['project_id'], 'window': window.model_dump(mode='json'),
               'query_sha256': hashlib.sha256(query.encode()).hexdigest(), 'query_template_version': 'posthog-event-outcome-v1',
               'fetched_at': datetime.now(timezone.utc).isoformat(), 'release': window.release}
    try:
        data = _request(_HOSTS[config['region']], 'POST', f"/api/projects/{config['project_id']}/query/", connection['secret'],
                        {'query': {'kind': 'HogQLQuery', 'query': query}, 'async': False, 'refresh': 'force_blocking', 'name': 'afterspan:event-outcome-v1'})
        values = _aggregate(data, _COLUMNS)
        total, universe = values['total'], values['universe_total']
        if (sum(values[k] for k in ('success', 'failure', 'unclassified')) != total or total > universe
                or values['environment_missing'] + total > universe or values['release_missing'] > universe
                or window.release and values['release_missing'] + total > universe):
            raise _Unavailable('malformed_counts')
    except _Unavailable as error:
        return receipt | {'provenance': 'source_gap', 'source_verified': False, 'reason': str(error),
                          'counts': None, 'coverage': {'status': 'unknown', 'source_total': None,
                                                      'environment_missing': None, 'release_missing': None}}
    coverage = {key: values[key] for key in ('environment_missing', 'release_missing')}
    coverage.update(source_total=universe, status='unknown' if any(coverage.values()) else 'complete')
    return receipt | {'provenance': 'provider_aggregate', 'source_verified': True,
                      'counts': {key: values[key] for key in _COLUMNS[:4]}, 'coverage': coverage,
                      'limitations': ['Counts describe logged events, not all real activity.',
                                      'Provider retention, sampling and collector delivery are not independently verified.']}


def railway_release(connection, commit, environment):
    if connection['kind'] != 'railway' or not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise HTTPException(422, 'Invalid release target')
    if not isinstance(environment, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', environment):
        raise HTTPException(422, 'Invalid release environment')
    config = _config('railway', connection['config'])
    receipt = {'provider': 'railway', 'status': 'pending', 'verified': False, 'commit': commit, 'environment': environment,
               **config, 'connection_id': connection['id'], 'connection_revision': connection['revision']}
    try:
        data = _request('backboard.railway.app', 'POST', '/graphql/v2', connection['secret'],
                        {'query': _RELEASE_QUERY, 'variables': {'serviceId': config['service_id'], 'environmentId': config['environment_id']}})
        if data.get('errors'):
            raise _Unavailable('provider_unavailable')
        node = data['data']['serviceInstance']['latestDeployment']
        if node is None:
            return receipt | {'reason': 'exact_deployment_not_found'}
        if (node.get('projectId') != config['project_id'] or node.get('serviceId') != config['service_id']
                or node.get('environmentId') != config['environment_id'] or node.get('environment') != {'id': config['environment_id'], 'name': environment}):
            return receipt | {'reason': 'deployment_target_mismatch'}
        if node.get('status') != 'SUCCESS':
            return receipt | {'reason': 'deployment_not_successful'}
        if not isinstance(node.get('meta'), dict) or node['meta'].get('commitHash') != commit:
            return receipt | {'reason': 'active_deployment_commit_mismatch'}
        released_at = SourceWindow.utc_timestamp(node.get('statusUpdatedAt'))
        if (released_at < SourceWindow.utc_timestamp(node['createdAt']) or released_at.timestamp() > time.time() + 5
                or not isinstance(node.get('id'), str) or not re.fullmatch(_UUID, node['id'])):
            raise _Unavailable('malformed_response')
        return receipt | {'status': 'released', 'verified': True, 'deployment_id': node['id'],
                          'released_at': released_at.isoformat(), 'timestamp_basis': 'provider_status_updated_at'}
    except _Unavailable as error:
        return receipt | {'reason': str(error)}
    except (KeyError, TypeError, ValueError, AttributeError):
        return receipt | {'reason': 'malformed_response'}


def validate_connection(kind, config, secret):
    config = _config(kind, config)
    try:
        if kind == 'github':
            data = _request('api.github.com', 'GET', '/repos/' + config['repository'], secret)
            if data.get('full_name') != config['repository'] or type(data.get('id')) is not int or data['id'] < 1:
                raise _Unavailable('target_unverified')
            extra = {'repository_id': data['id']}
        elif kind == 'posthog':
            data = _request(_HOSTS[config['region']], 'POST', f"/api/projects/{config['project_id']}/query/", secret,
                            {'query': {'kind': 'HogQLQuery', 'query': 'SELECT count() AS total FROM events WHERE 1 = 0 LIMIT 1'}, 'async': False})
            if _aggregate(data, ['total']) != {'total': 0}:
                raise _Unavailable('target_unverified')
            extra = {}
        else:
            query = '''query AfterspanTarget($project: String!, $environment: String!, $service: String!) {
                project(id: $project) { id } environment(id: $environment) { id projectId }
                service(id: $service) { id projectId }
            }'''
            data = _request('backboard.railway.app', 'POST', '/graphql/v2', secret,
                            {'query': query, 'variables': {key.removesuffix('_id'): value for key, value in config.items()}})
            if (data.get('errors') or data.get('data') != {'project': {'id': config['project_id']},
                    'environment': {'id': config['environment_id'], 'projectId': config['project_id']},
                    'service': {'id': config['service_id'], 'projectId': config['project_id']}}):
                raise _Unavailable('target_unverified')
            extra = {}
    except _Unavailable:
        raise HTTPException(503, 'Provider read access could not be verified') from None
    return {'kind': kind, 'target': config, 'read_access': 'verified', 'credential_scope': 'unverified', **extra}
