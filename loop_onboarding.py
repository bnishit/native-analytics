import hashlib
import json
import math
import time
import uuid
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, SecretStr, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

from product_analytics import Model
from loop_measurement import event_matches


Scalar = StrictBool | StrictInt | StrictFloat | StrictStr
Property = Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')]
Path = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,159}$')]
SHA = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def same_scalar(a, b):
    return a == b and (type(a) is type(b) or type(a) in (int, float) and type(b) in (int, float))


def bounded_scalar(value):
    if (isinstance(value, str) and (len(value) > 256 or any(ord(c) < 32 for c in value))
            or type(value) in (int, float) and (not math.isfinite(value) or abs(value) > 2 ** 53 - 1)):
        raise ValueError('use a bounded finite scalar')
    return value


class SourceInput(Model):
    kind: Literal['github', 'posthog', 'railway']
    config: dict
    credential: SecretStr


class ScanInput(Model):
    connection_id: uuid.UUID
    commit: SHA | None = None
    base: str = Field(default='main', pattern=r'^[A-Za-z0-9][A-Za-z0-9_./-]{0,150}$')

    @field_validator('base')
    @classmethod
    def safe_base(cls, value):
        if '..' in value or '//' in value or value.endswith(('/', '.lock')):
            raise ValueError('invalid branch')
        return value


class Filter(Model):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    property: Property
    op: Literal['eq', 'in'] = 'eq'
    value: Scalar | list[Scalar]

    @model_validator(mode='after')
    def typed_value(self):
        if self.op == 'in' and (not isinstance(self.value, list) or not 1 <= len(self.value) <= 20):
            raise ValueError('one-of needs one to twenty scalar values')
        if self.op == 'eq' and isinstance(self.value, list):
            raise ValueError('equality needs one scalar value')
        if len(canonical(self.value)) > 2000:
            raise ValueError('filter is too large')
        for value in self.value if isinstance(self.value, list) else [self.value]:
            bounded_scalar(value)
        return self


class EventOutcome(Model):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    kind: Literal['event-outcome-v1']
    unit: Literal['logged_event']
    event: str = Field(pattern=r'^[A-Za-z0-9_$][A-Za-z0-9_.$:@/-]{0,159}$')
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    outcome_property: Property
    success_values: list[Scalar] = Field(min_length=1, max_length=20)
    failure_values: list[Scalar] = Field(min_length=1, max_length=20)
    min_sample_size: int = Field(default=25, ge=1, le=100000, strict=True)

    @model_validator(mode='after')
    def disjoint(self):
        values = self.success_values + self.failure_values
        for value in values:
            bounded_scalar(value)
        if any(same_scalar(value, other) for index, value in enumerate(values) for other in values[index + 1:]):
            raise ValueError('success and failure values must be disjoint and unique')
        return self


class NativeSource(Model):
    kind: Literal['native']
    environment: Literal['production', 'staging', 'development', 'test', 'local']
    environment_property: Property = 'environment'
    release_property: Property


class PostHogSource(NativeSource):
    kind: Literal['posthog']
    connection_id: uuid.UUID
    connection_revision: int = Field(ge=1, strict=True)


class ReviewedTracking(Model):
    kind: Literal['reviewed-artifact-v1']
    dependencies: list[Path] = Field(min_length=1, max_length=16)

    @field_validator('dependencies')
    @classmethod
    def safe_paths(cls, values):
        if len(values) != len(set(values)) or any('..' in value.split('/') or '//' in value for value in values):
            raise ValueError('use unique safe relative dependency paths')
        return values


class RepoGoal(Model):
    schema_version: Literal['afterspan-repo-goal-v2'] = Field(alias='schema')
    id: str = Field(pattern=r'^[a-z][a-z0-9-]{0,63}$')
    question: str = Field(min_length=1, max_length=2000)
    scan_id: uuid.UUID
    measurement: EventOutcome
    mapping: Annotated[NativeSource | PostHogSource, Field(discriminator='kind')]
    tracking: ReviewedTracking


class AdoptInput(Model):
    proposal_id: uuid.UUID
    proposal_sha256: Digest


class ReleaseInput(Model):
    connection_id: uuid.UUID


def repo_goal(current):
    return current['bundle']['definition'].get('schema') == 'afterspan-repo-goal-v2'


class RepoOnboarding:
    @staticmethod
    def init_db(c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS loop_repo_receipts(
          project TEXT NOT NULL,id TEXT NOT NULL,kind TEXT NOT NULL,input_sha256 TEXT NOT NULL,
          receipt TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,id));
        CREATE TABLE IF NOT EXISTS loop_repo_jobs(
          project TEXT NOT NULL,id TEXT NOT NULL,kind TEXT NOT NULL,input_sha256 TEXT NOT NULL,
          status TEXT NOT NULL,result TEXT,created REAL NOT NULL,PRIMARY KEY(project,id));
        ''')
        c.execute("UPDATE loop_repo_jobs SET status='interrupted' WHERE status='pending'")

    def __init__(self, loop, app, reader, editor, verify_release):
        from loop_sources import Connections
        self.loop = loop
        self.verify_release = verify_release
        self.access_check = ContextVar('afterspan_repo_access', default=None)
        self.connections = Connections(loop.connect)

        @app.get('/api/loop/sources', dependencies=[Depends(reader)])
        def sources(project: str):
            return {'connections': self.connections.list(project)}

        @app.put('/api/loop/sources/{id}')
        def connect_source(project: str, id: uuid.UUID, revision: int, req: SourceInput, request: Request):
            from loop_sources import validate_connection
            self.require_owner(request, project)
            checked = validate_connection(req.kind, req.config, req.credential)
            result = self.connections.put(project, str(id), revision, req.kind, req.config, req.credential)
            return {**result, 'access_check': checked}

        @app.delete('/api/loop/sources/{id}')
        def revoke_source(project: str, id: uuid.UUID, revision: int, request: Request):
            self.require_owner(request, project)
            return self.connections.revoke(project, str(id), revision)

        @app.put('/api/loop/scans/{id}', dependencies=[Depends(editor)])
        def scan(project: str, id: uuid.UUID, req: ScanInput, background: BackgroundTasks, request: Request):
            payload = req.model_dump(mode='json')
            return self.queue(project, str(id), 'scan', payload, background, lambda: self.scan(project, str(id), req),
                              lambda: editor(request, project))

        @app.get('/api/loop/scans/{id}', dependencies=[Depends(reader)])
        def scan_status(project: str, id: uuid.UUID):
            try:
                return self.receipt(project, str(id), 'scan')
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                return self.job(project, str(id))

        @app.get('/api/loop/jobs/{id}', dependencies=[Depends(reader)])
        def job_status(project: str, id: uuid.UUID):
            return self.job(project, str(id))

        @app.put('/api/loop/proposals/{id}', dependencies=[Depends(editor)])
        def propose(project: str, id: uuid.UUID, req: RepoGoal):
            return self.propose(project, str(id), req)

        @app.post('/api/loop/goals/{goal}/adopt')
        def adopt(project: str, goal: str, req: AdoptInput, request: Request):
            self.require_owner(request, project)
            return self.adopt(project, goal, req)

        @app.post('/api/loop/actions/{id}/release', dependencies=[Depends(editor)])
        def release(project: str, id: uuid.UUID, req: ReleaseInput, request: Request):
            with self.authorized(lambda: editor(request, project)):
                return self.release(project, str(id), str(req.connection_id))

    def require_owner(self, request, project):
        self.loop.owner(request, project)
        if request.headers.get('authorization', '').startswith('Bearer as_workspace_'):
            raise HTTPException(403, 'an owner account session is required; delegated keys cannot grant access or adopt goals')

    @contextmanager
    def authorized(self, check):
        check()
        token = self.access_check.set(check)
        try:
            yield
        finally:
            self.access_check.reset(token)

    def job(self, project, id):
        with closing(self.loop.connect()) as c:
            row = c.execute('SELECT kind,status,result FROM loop_repo_jobs WHERE project=? AND id=?', (project, id)).fetchone()
        if not row:
            raise HTTPException(404, 'job not found')
        return {'id': id, 'kind': row['kind'], 'status': row['status'],
                'result': json.loads(row['result']) if row['result'] else None,
                'next_action': 'read_result' if row['status'] == 'complete' else 'retry_same_request' if row['status'] in {'failed', 'interrupted'} else 'check_job'}

    def queue(self, project, id, kind, payload, background, operation, authorize):
        authorize()
        with closing(self.loop.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM loop_repo_jobs WHERE project=? AND id=?', (project, id)).fetchone()
            if row and (row['kind'] != kind or row['input_sha256'] != digest(payload)):
                raise HTTPException(409, 'job ID belongs to different input')
            if row and row['status'] == 'complete':
                return json.loads(row['result'])
            if not row:
                if c.execute("SELECT count(*) FROM loop_repo_jobs WHERE project=? AND status='pending'", (project,)).fetchone()[0] >= 3:
                    raise HTTPException(429, 'three repository reads are pending; finish or resume them before starting another')
                c.execute('INSERT INTO loop_repo_jobs VALUES(?,?,?,?,?,?,?)',
                          (project, id, kind, digest(payload), 'pending', None, time.time()))
            elif row['status'] in {'failed', 'interrupted'}:
                c.execute("UPDATE loop_repo_jobs SET status='pending',result=NULL WHERE project=? AND id=?", (project, id))
        background.add_task(self.run_job, project, id, operation, authorize)
        return JSONResponse({'id': id, 'status': 'pending', 'kind': kind,
                             'status_path': '/api/loop/jobs/' + id, 'next_action': 'check_job'}, status_code=202)

    def run_job(self, project, id, operation, authorize):
        lock = self.loop.action_locks[(project, 'job', id)]
        if not lock.acquire(blocking=False):
            return
        try:
            if self.job(project, id)['status'] == 'complete':
                return
            try:
                with self.authorized(authorize):
                    result, status = operation(), 'complete'
            except Exception:
                result, status = {'error': 'Provider operation did not complete. Retry the same request; no success was inferred.'}, 'failed'
            with closing(self.loop.connect()) as c, c:
                c.execute('UPDATE loop_repo_jobs SET status=?,result=? WHERE project=? AND id=?',
                          (status, canonical(result), project, id))
        finally:
            lock.release()

    def receipt(self, project, id, kind):
        with closing(self.loop.connect()) as c:
            row = c.execute('SELECT * FROM loop_repo_receipts WHERE project=? AND id=? AND kind=?', (project, id, kind)).fetchone()
        if row is None:
            raise HTTPException(404, 'receipt not found')
        return json.loads(row['receipt'])

    def replay(self, project, id, kind, payload):
        with closing(self.loop.connect()) as c:
            row = c.execute('SELECT * FROM loop_repo_receipts WHERE project=? AND id=?', (project, id)).fetchone()
        if row:
            if row['kind'] != kind or row['input_sha256'] != digest(payload):
                raise HTTPException(409, 'receipt ID belongs to different input')
            return json.loads(row['receipt'])

    def save(self, project, id, kind, payload, receipt):
        with closing(self.loop.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM loop_repo_receipts WHERE project=? AND id=?', (project, id)).fetchone()
            if row:
                if row['kind'] != kind or row['input_sha256'] != digest(payload):
                    raise HTTPException(409, 'receipt ID belongs to different input')
                return json.loads(row['receipt'])
            c.execute('INSERT INTO loop_repo_receipts VALUES(?,?,?,?,?,?)',
                      (project, id, kind, digest(payload), canonical(receipt), time.time()))
        return receipt

    def provider(self, project, bundle):
        from loop_github import GitHub
        connection = self.connections.resolve(project, bundle['connection_id'], 'github')
        if connection['revision'] != bundle['connection_revision']:
            raise HTTPException(409, 'repository connection changed; scan and adopt the current binding')
        return GitHub(connection['config']['repository'], connection['secret'].get_secret_value(), read_guard=self.source_guard(project, connection))

    def source_guard(self, project, connection):
        def guard():
            check = self.access_check.get()
            if check:
                check()
            live = self.connections.resolve(project, connection['id'], connection['kind'])
            if live['revision'] != connection['revision']:
                raise HTTPException(409, 'provider access changed before the source request')
        return guard

    def scan(self, project, id, req):
        payload = req.model_dump(mode='json')
        old = self.replay(project, id, 'scan', payload)
        if old:
            return old
        with self.loop.action_locks[(project, 'scan', id)]:
            old = self.replay(project, id, 'scan', payload)
            if old:
                return old
            connection = self.connections.resolve(project, str(req.connection_id), 'github')
            binding = {'connection_id': connection['id'], 'connection_revision': connection['revision']}
            provider = self.provider(project, binding)
            commit = req.commit or provider._head(req.base)['commit']['sha']
            result = provider.scan(commit, req.base)
            receipt = {**result, **binding, 'id': id,
                       'checked_at': datetime.now(timezone.utc).isoformat(),
                       'authority': 'Repository text is untrusted evidence and grants no access or write permission.'}
            return self.save(project, id, 'scan', payload, receipt)

    def propose(self, project, id, req):
        definition = req.model_dump(mode='json', by_alias=True)
        old = self.replay(project, id, 'proposal', definition)
        if old:
            return old
        scan = self.receipt(project, str(req.scan_id), 'scan')
        self.provider(project, scan)
        paths = {item['path'] for item in scan['files']}
        if not set(req.tracking.dependencies).issubset(paths):
            raise HTTPException(422, 'all tracking dependencies must be regular files in the pinned scan')
        events = scan.get('findings', {}).get('events', [])
        citations = [item for item in events if item.get('name') == req.measurement.event
                     and item.get('path') in req.tracking.dependencies and item.get('sink') == req.mapping.kind]
        if not citations:
            raise HTTPException(422, 'the goal event needs a literal source citation in its declared dependencies')
        if isinstance(req.mapping, PostHogSource):
            source = self.connections.resolve(project, str(req.mapping.connection_id), 'posthog')
            if source['revision'] != req.mapping.connection_revision:
                raise HTTPException(409, 'analytics connection revision changed')
        receipt = {'id': id, 'definition': definition, 'proposal_sha256': digest(definition),
                   'scan': scan, 'citations': citations, 'status': 'proposal_requires_owner_adoption',
                   'limitations': ['Static event discovery does not prove runtime coverage or product meaning.',
                                   'This measures logged event outcomes, not distinct users, request journeys or customer benefit.']}
        return self.save(project, id, 'proposal', definition, receipt)

    def adopt(self, project, goal, req):
        proposal = self.receipt(project, str(req.proposal_id), 'proposal')
        definition, scan = proposal['definition'], proposal['scan']
        if definition['id'] != goal or proposal['proposal_sha256'] != req.proposal_sha256:
            raise HTTPException(409, 'adopt the exact goal ID and proposal digest')
        self.provider(project, scan)
        bundle = {**scan, 'definition': definition, 'path': '.afterspan/goals/' + goal + '.json',
                  'content_sha256': hashlib.sha256((canonical(definition) + '\n').encode()).hexdigest(),
                  'proposal_sha256': req.proposal_sha256, 'provenance': 'owner_adopted_scan_bound_proposal'}
        with closing(self.loop.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM loop_goals WHERE project=? AND goal=? ORDER BY revision DESC LIMIT 1', (project, goal)).fetchone()
            if old and json.loads(old['bundle']) == bundle:
                return self.loop.view(project, goal, c)
            if old and json.loads(old['bundle'])['definition'].get('schema') != 'afterspan-repo-goal-v2':
                raise HTTPException(409, 'use a new goal ID; the existing native contract cannot be replaced')
            if not old and c.execute('SELECT count(DISTINCT goal) FROM loop_goals WHERE project=?', (project,)).fetchone()[0] >= 100:
                raise HTTPException(422, 'maximum 100 goals per project')
            c.execute('INSERT INTO loop_goals VALUES(?,?,?,?,?,?,?,?)',
                      (project, goal, old['revision'] + 1 if old else 1, digest(definition['question']),
                       digest(definition['measurement']), digest(definition['mapping']), canonical(bundle), time.time()))
            return self.loop.view(project, goal, c)

    def prepare(self, project, goal, request_id, files):
        if not files:
            raise HTTPException(422, 'provide a bounded tracking artifact for independent review')
        payload = {'goal': goal, 'files': files}
        with self.loop.action_locks[(project, 'prepare', goal, request_id)]:
            self.save(project, request_id, 'artifact_input', payload, {'request_sha256': digest(payload)})
            with closing(self.loop.connect()) as c, c:
                c.execute('BEGIN IMMEDIATE')
                current = self.loop.goal(c, project, goal)
                old = c.execute('SELECT id,goal_revision FROM loop_actions WHERE project=? AND goal=? AND request_id=?',
                                (project, goal, request_id)).fetchone()
                if old:
                    if old['goal_revision'] != current['revision']:
                        raise HTTPException(409, 'request ID belongs to an older goal revision')
                    action = self.loop.action(c, project, old['id'])
                    if action['plan'] is not None:
                        return action
                    id = old['id']
                else:
                    id = str(uuid.uuid4())
                    c.execute('INSERT INTO loop_actions VALUES(?,?,?,?,?,?,?,?,?)',
                              (project, goal, id, request_id, current['revision'], None, None, 'preparing', time.time()))
            bundle = current['bundle']
            plan = self.provider(project, bundle).prepare_artifact(bundle, bundle['definition'], files)
            plan = {**plan, 'status': 'prepared', 'id': id, 'action_id': id, **self.loop.refs(current)}
            with closing(self.loop.connect()) as c, c:
                c.execute('UPDATE loop_actions SET plan=?,status=? WHERE project=? AND id=?',
                          (canonical(plan), 'prepared', project, id))
                return self.loop.action(c, project, id)

    def release(self, project, id, connection_id):
        from loop_sources import railway_release
        with self.loop.action_locks[(project, id)]:
            with closing(self.loop.connect()) as c:
                action = self.loop.action(c, project, id)
                current = self.loop.goal(c, project, action['goal'])
            if not repo_goal(current) or not action['receipt'] or not action['receipt'].get('merged'):
                raise HTTPException(409, 'a reconciled merged repository artifact is required')
            if current['revision'] != action['goal_revision']:
                raise HTTPException(409, 'goal changed since this artifact was reviewed')
            provider = self.provider(project, current['bundle'])
            receipt = provider.reconcile(action['plan'], action['receipt'])
            if receipt.get('merged') is not True or receipt.get('artifact_sha256') != action['plan']['artifact_sha256']:
                raise HTTPException(409, 'the merged artifact could not be verified')
            connection = self.connections.resolve(project, connection_id, 'railway')
            environment = current['bundle']['definition']['mapping']['environment']
            proof = railway_release(connection, receipt['merge_sha'], environment, guard=self.source_guard(project, connection))
            if proof.get('status') == 'released' and proof.get('verified') is True:
                receipt = {**receipt, 'release_status': 'released', 'release': {
                    **proof, 'artifact_sha256': action['plan']['artifact_sha256'],
                    'connection_id': connection['id'], 'connection_revision': connection['revision']}}
            else:
                receipt = {**receipt, 'release_status': 'release_pending', 'release': None,
                           'release_gap': proof.get('reason', 'exact successful Railway deployment not observed')}
            with closing(self.loop.connect()) as c, c:
                c.execute('UPDATE loop_actions SET receipt=?,status=? WHERE project=? AND id=?',
                          (canonical(receipt), receipt.get('status', 'merged'), project, id))
                return self.loop.action(c, project, id)

    def native_observe(self, project, definition, window):
        mapping, metric = definition['mapping'], definition['measurement']
        start, end, cutoff = [datetime.fromisoformat(window[key]).timestamp() for key in ['entry_start', 'entry_end', 'as_of']]
        with closing(self.loop.connect()) as c:
            rows = c.execute('''SELECT id,name,properties FROM events WHERE project=? AND name=?
                AND ts>=? AND ts<? AND received<=? ORDER BY ts,id LIMIT 200001''',
                (project, metric['event'], start, end, cutoff)).fetchall()
            erasure = c.execute('SELECT 1 FROM erased_identities WHERE project=? AND erased>=? LIMIT 1', (project, start)).fetchone()
        if len(rows) > 200000:
            raise HTTPException(422, 'shorten the window below 200,000 candidate events')
        counts = dict.fromkeys(['total', 'success', 'failure', 'unclassified'], 0)
        coverage = {'source_total': 0, 'environment_missing': 0, 'release_missing': 0, 'status': 'bounded_read'}
        for row in rows:
            properties = json.loads(row['properties'])
            event = {'name': row['name'], 'properties': properties}
            if not event_matches(event, {'event': metric['event'], 'filters': metric['filters']}):
                continue
            coverage['source_total'] += 1
            environment = properties.get(mapping['environment_property'])
            if not isinstance(environment, str) or not environment.strip() or environment == '[redacted]':
                coverage['environment_missing'] += 1
            if environment != mapping['environment']:
                continue
            if window.get('release'):
                release = properties.get(mapping['release_property'])
                if not isinstance(release, str) or not release.strip() or release == '[redacted]':
                    coverage['release_missing'] += 1
                if release != window['release']:
                    continue
            counts['total'] += 1
            outcome = properties.get(metric['outcome_property'])
            def matches(values):
                return any(same_scalar(outcome, value) for value in values)
            key = 'success' if matches(metric['success_values']) else 'failure' if matches(metric['failure_values']) else 'unclassified'
            counts[key] += 1
        retention = start < time.time() - self.loop.retention_days * 86400
        return {'provenance': 'native_service_read', 'source_verified': True, 'counts': counts, 'coverage': {
            **coverage, 'retention_gap': retention, 'erasure_observed': bool(erasure)},
            'query_sha256': digest({'contract': 'event-outcome-v1', 'definition': definition, 'window': window}),
            'response_sha256': digest({'counts': counts, 'coverage': coverage}),
            'fetched_at': datetime.now(timezone.utc).isoformat(), 'window': window}

    def observe(self, project, goal, id, req):
        from loop_sources import posthog_observe
        window = req.model_dump(mode='json')
        fingerprint = digest(window)
        with self.loop.action_locks[(project, 'observe', id)]:
            with closing(self.loop.connect()) as c:
                old = self.loop.existing_receipt(c, 'loop_observations', project, goal, id, fingerprint)
                if old:
                    return old
                current = self.loop.goal(c, project, goal)
            definition = current['bundle']['definition']
            self.provider(project, current['bundle'])
            if definition['mapping']['kind'] == 'native':
                result = self.native_observe(project, definition, window)
                source = 'native'
            else:
                connection = self.connections.resolve(project, definition['mapping']['connection_id'], 'posthog')
                if connection['revision'] != definition['mapping']['connection_revision']:
                    raise HTTPException(409, 'analytics connection changed; adopt a new mapping')
                result = posthog_observe(connection, definition, {key: window[key] for key in ['entry_start', 'entry_end', 'as_of', 'release']},
                                         guard=self.source_guard(project, connection))
                source = 'provider_verified' if result.get('source_verified') else 'source_gap'
            counts = result.get('counts')
            coverage = result.get('coverage', {})
            gap = (not counts or not result.get('source_verified') or counts['unclassified'] > 0
                   or coverage.get('environment_missing', 0) > 0 or coverage.get('release_missing', 0) > 0
                   or coverage.get('retention_gap') or coverage.get('erasure_observed'))
            enough = bool(counts and counts['total'] >= definition['measurement']['min_sample_size'])
            verdict = 'source_gap' if not counts else 'coverage_gap' if gap else 'observed' if enough else 'insufficient_evidence'
            receipt = {**result, 'schema': 'afterspan-observation-v2', 'id': id, 'goal_id': goal,
                       **self.loop.refs(current), 'source': source, 'environment': definition['mapping']['environment'],
                       'window': window, 'verdict': verdict,
                       'rate': counts['success'] / counts['total'] if counts and counts['total'] and not gap and enough else None,
                       'release_bound': False, 'next_action': 'collect_evidence' if not enough or gap else 'no_change',
                       'limitations': [*result.get('limitations', []), 'The unit is logged events, not distinct people or completed customer journeys.',
                                       'Before/after is observational and cannot prove causal benefit.',
                                       'A source read proves retained observations, not complete collection or human usefulness.']}
            with closing(self.loop.connect()) as c, c:
                c.execute('BEGIN IMMEDIATE')
                live = self.loop.goal(c, project, goal)
                if live['revision'] != current['revision']:
                    raise HTTPException(409, 'goal changed during the source read; use a new observation')
                old = self.loop.existing_receipt(c, 'loop_observations', project, goal, id, fingerprint)
                if old:
                    return old
                for row in c.execute('SELECT id FROM loop_actions WHERE project=? AND goal=? AND goal_revision=? ORDER BY created DESC LIMIT 100',
                                     (project, goal, current['revision'])):
                    release = self.verify_release(self.loop.action(c, project, row['id']), receipt)
                    if (release and window.get('release') == release['commit']
                            and datetime.fromisoformat(window['entry_start']) >= datetime.fromisoformat(release['released_at'])):
                        receipt['release_bound'] = bool(counts and counts['total'] > 0 and not gap)
                        receipt['release_proof'] = release
                        break
                c.execute('INSERT INTO loop_observations VALUES(?,?,?,?,?,?)',
                          (project, goal, id, fingerprint, canonical(receipt), time.time()))
            return receipt

    def view(self, current, policy, actions, observations, history):
        definition = current['bundle']['definition']
        latest = next((a for a in actions if a['goal_revision'] == current['revision']), None)
        action_receipt = latest['receipt'] or {} if latest else {}
        release = self.verify_release(latest, {**self.loop.refs(current), 'environment': definition['mapping']['environment']}) if latest else None
        exact = bool(release)
        current_observations = [o for o in observations if o['measurement_revision'] == current['measurement_revision']
                                and o['mapping_revision'] == current['mapping_revision']]
        bound = [o for o in current_observations if exact and o['window'].get('release') == release['commit']
                 and datetime.fromisoformat(o['window']['entry_start']) >= datetime.fromisoformat(release['released_at'])]
        signal = any(o.get('counts') and o['counts']['total'] > 0 for o in bound)
        outcome = any(o['verdict'] == 'observed' for o in bound)
        stages = {'tracking': 'prepared' if latest and latest['plan'] else 'not_prepared',
                  'pr': 'observed' if action_receipt.get('pr_number') else 'not_observed',
                  'merge': 'merged' if action_receipt.get('merged') else 'not_observed',
                  'release': 'released' if exact else 'not_observed',
                  'signal': 'verified_release_event_observed' if signal else 'not_observed',
                  'outcome': 'observed_for_verified_release' if outcome else 'insufficient_evidence' if bound else 'not_observed'}
        following = ('prepare_tracking' if not latest else 'review_pr' if not action_receipt.get('merged')
                     else 'check_release' if not exact else 'collect_evidence' if not outcome else 'no_change')
        return {'goal': {'id': definition['id'], 'question': definition['question'], 'source': current['bundle'],
                         'definition': definition, **self.loop.refs(current)}, 'history': history,
                'policy': {k: v for k, v in policy.items() if k != 'issuer'} if policy else None,
                'actions': actions, 'observations': observations, 'connection': None, 'connection_status': None,
                'latest_answer': None, 'dependency_status': 'scan_bound_proposal', 'stages': stages,
                'next_action': following, 'tracking_repair_available': False,
                'tracking_artifact_available': True, 'limitations': [
                    'Tracking code requires review. Exact artifact verification does not certify its business meaning.',
                    'An exact released event is separate from enough evidence that the change helped users.']}

    def compare(self, before, after, release, action_id):
        reasons = []
        if before.get('schema') != 'afterspan-observation-v2' or after.get('schema') != 'afterspan-observation-v2':
            return {'status': 'unknown', 'reasons': ['measurement_contract_changed'], 'rate_difference': None,
                    'post_release_comparison': False, 'action_id': str(action_id) if action_id else None,
                    'limitations': ['Repository event outcomes and native checked answers have different measurement contracts.']}
        for key in ['source', 'measurement_revision', 'mapping_revision', 'environment']:
            if before.get(key) != after.get(key):
                reasons.append(key + '_changed')
        if not release:
            reasons.append('exact_release_proof_missing')
        else:
            if before['window'].get('release') != release.get('base_commit'):
                reasons.append('baseline_release_unbound')
            if after['window'].get('release') != release['commit']:
                reasons.append('after_release_unbound')
            released = datetime.fromisoformat(release['released_at'])
            if datetime.fromisoformat(before['window']['entry_end']) > released:
                reasons.append('baseline_overlaps_release')
            if datetime.fromisoformat(after['window']['entry_start']) < released:
                reasons.append('after_precedes_release')
        if datetime.fromisoformat(before['window']['entry_end']) > datetime.fromisoformat(after['window']['entry_start']):
            reasons.append('windows_overlap')
        for label, receipt in [('before', before), ('after', after)]:
            if receipt.get('verdict') != 'observed' or receipt.get('rate') is None:
                reasons.append(label + '_evidence_insufficient')
            if receipt.get('coverage', {}).get('received_cutoff_supported') is False:
                reasons.append(label + '_provider_receipt_cutoff_unavailable')
        return {'status': 'unknown' if reasons else 'comparable', 'reasons': reasons,
                'rate_difference': None if reasons else after['rate'] - before['rate'],
                'post_release_comparison': not reasons, 'action_id': str(action_id) if action_id else None,
                'limitations': ['Logged event outcomes are not unique-user conversion.',
                                'Before/after cannot prove the release caused a change.']}
