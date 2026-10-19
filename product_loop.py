import hashlib
import json
import threading
import time
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from pydantic import Field, ValidationError, field_validator, model_validator

from product_analytics import EventMatch, Model
from loop_measurement import LIMITATIONS, check_answer, compare, eligible, count_first_entry_outcomes, event_matches, next_action


Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Commit = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]
EventID = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}$')]
GoalID = Annotated[str, Field(pattern=r'^[a-z][a-z0-9-]{0,63}$')]
NextAction = Literal['sync_goal', 'prepare_tracking', 'review_pr', 'check_release', 'collect_evidence',
                     'review_answers', 'keep_observing', 'no_change']
Environment = Literal['local', 'test', 'development', 'staging', 'production']
EVENT_CAP = 200000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def verified_release(action, evidence):
    receipt, plan = action['receipt'] or {}, action['plan'] or {}
    release = receipt.get('release') or {}
    if (receipt.get('merged') is True and receipt.get('release_status') == 'released'
            and release.get('verified') is True and release.get('released_at') is not None
            and plan.get('artifact_sha256') and receipt.get('merge_sha')
            and plan.get('measurement_revision') == evidence['measurement_revision']
            and plan.get('mapping_revision') == evidence['mapping_revision']
            and receipt.get('artifact_sha256') == plan.get('artifact_sha256')
            and release.get('commit') == receipt.get('merge_sha')
            and release.get('artifact_sha256') == plan.get('artifact_sha256')
            and release.get('environment') == evidence['environment']):
        return {**release, 'base_commit': plan['base_sha']}


class GoalSource(Model):
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
    base: str = Field(min_length=1, max_length=160, pattern=r'^[A-Za-z0-9][A-Za-z0-9_./-]*$')
    commit: Commit
    path: str = Field(pattern=r'^\.afterspan/goals/[a-z][a-z0-9-]{0,63}\.json$')
    content_sha256: Digest

    @field_validator('base')
    @classmethod
    def safe_base(cls, value):
        if '..' in value or '//' in value or value.endswith(('/', '.lock')):
            raise ValueError('invalid branch')
        return value


class Measurement(Model):
    unit: Literal['workspace']
    entry: EventMatch
    completion: EventMatch
    followup_seconds: Literal[86400]
    entry_rule: Literal['first_in_entry_window']
    rubric: Literal['aggregate-answer-v1']


class Mapping(Model):
    source: Literal['native']
    environment: Environment
    observation_id_property: Literal['observation_id']


class Tracking(Model):
    recipe: Literal['python-answer-v1']
    path: Literal['examples/loop_pilot.py']
    dependencies: list[Literal['examples/loop_pilot.py']] = Field(min_length=1, max_length=1)


class GoalDefinition(Model):
    schema_version: Literal['afterspan-goal-v1'] = Field(alias='schema')
    id: GoalID
    question: str = Field(min_length=1, max_length=2000)
    measurement: Measurement
    mapping: Mapping
    tracking: Tracking


class RequiredCheck(Model):
    name: str = Field(min_length=1, max_length=160)
    app_id: int = Field(ge=1, strict=True)
    workflow_path: str = Field(pattern=r'^\.github/workflows/[A-Za-z0-9_.-]{1,100}\.ya?ml$')
    workflow_sha: Commit


class LoopPolicy(Model):
    state: Literal['active', 'paused', 'revoked']
    repository: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
    base: str = Field(min_length=1, max_length=160)
    operations: list[Literal['draft-pr', 'merge', 'release-on-merge']] = Field(max_length=3)
    paths: list[Literal['examples/loop_pilot.py']] = Field(min_length=1, max_length=1)
    environment: Environment
    required_checks: list[RequiredCheck] = Field(default_factory=list, max_length=10)
    expires_at: datetime

    @field_validator('expires_at')
    @classmethod
    def aware_expiry(cls, value):
        if value.tzinfo is None:
            raise ValueError('use a timestamp with a timezone')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def unique_operations(self):
        if len(self.operations) != len(set(self.operations)):
            raise ValueError('duplicate operations')
        if 'merge' in self.operations and not self.required_checks:
            raise ValueError('merge authority needs pinned independent checks')
        return self


class Prepare(Model):
    request_id: uuid.UUID


class Empty(Model):
    pass


class ObservationWindow(Model):
    entry_start: datetime
    entry_end: datetime
    observed_through: datetime
    as_of: datetime
    release: Commit | None = None

    @field_validator('entry_start', 'entry_end', 'observed_through', 'as_of')
    @classmethod
    def aware_window(cls, value):
        if value.tzinfo is None:
            raise ValueError('use a timestamp with a timezone')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def bounded_window(self):
        if not 0 < (self.entry_end - self.entry_start).total_seconds() <= 90 * 86400:
            raise ValueError('entry window must be positive and at most 90 days')
        if not self.entry_end <= self.observed_through <= self.as_of:
            raise ValueError('entry_end <= observed_through <= as_of required')
        if self.as_of.timestamp() > time.time() + 1:
            raise ValueError('snapshot is in the future')
        return self


class AggregateAnswer(Model):
    source: Literal['native', 'externally_attested']
    measurement_revision: Digest
    mature: int = Field(ge=0, le=EVENT_CAP, strict=True)
    qualified: int = Field(ge=0, le=EVENT_CAP, strict=True)
    waiting: int = Field(ge=0, le=EVENT_CAP, strict=True)
    unassessed: int = Field(ge=0, le=EVENT_CAP, strict=True)
    rate: float | None = Field(ge=0, le=1, strict=True)
    limitations: list[str] = Field(max_length=20)
    next_action: NextAction

    @field_validator('limitations')
    @classmethod
    def bounded_limitations(cls, value):
        if any(len(item) > 500 for item in value):
            raise ValueError('limitation exceeds 500 characters')
        return value


class AnswerCheck(Model):
    completion_event_id: EventID
    evidence_id: uuid.UUID
    answer: AggregateAnswer


class ExternalObservation(ObservationWindow):
    kind: Literal['aggregate'] = 'aggregate'
    measurement_revision: Digest
    mapping_revision: Digest
    source_url: str = Field(min_length=1, max_length=1000)
    query_sha256: Digest
    entrants: int = Field(ge=0, le=EVENT_CAP, strict=True)
    mature: int = Field(ge=0, le=EVENT_CAP, strict=True)
    waiting: int = Field(ge=0, le=EVENT_CAP, strict=True)
    qualified: int = Field(ge=0, le=EVENT_CAP, strict=True)
    failed: int = Field(ge=0, le=EVENT_CAP, strict=True)
    unassessed: int = Field(ge=0, le=EVENT_CAP, strict=True)
    timed_out: int = Field(ge=0, le=EVENT_CAP, strict=True)
    technical_failures: int = Field(ge=0, le=EVENT_CAP, strict=True)
    rate: float | None = Field(ge=0, le=1, strict=True)

    @field_validator('source_url')
    @classmethod
    def safe_url(cls, value):
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('use an HTTPS provenance URL without credentials, query or fragment')
        return value

    @model_validator(mode='after')
    def consistent_counts(self):
        if self.entrants != self.mature + self.waiting:
            raise ValueError('entrants must equal mature plus waiting')
        if self.mature != self.qualified + self.failed + self.unassessed + self.timed_out:
            raise ValueError('mature partition does not match')
        expected = self.qualified / self.mature if self.mature and not self.unassessed else None
        if self.rate != expected:
            raise ValueError('rate does not match the attested counts')
        return self


class ExternalContractGap(Model):
    kind: Literal['contract_gap']
    source_url: str = Field(min_length=1, max_length=1000)
    query_sha256: Digest
    checked_at: datetime
    missing_events: list[EventID] = Field(min_length=1, max_length=20)

    _safe_url = field_validator('source_url')(ExternalObservation.safe_url.__func__)

    @field_validator('checked_at')
    @classmethod
    def checked_time(cls, value):
        if value.tzinfo is None or value.timestamp() > time.time() + 1:
            raise ValueError('use a past timestamp with a timezone')
        return value.astimezone(timezone.utc)


class Comparison(Model):
    before: uuid.UUID
    after: uuid.UUID
    action_id: uuid.UUID | None = None


class ProductLoop:
    @staticmethod
    def init_db(c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS loop_goals(
          project TEXT NOT NULL REFERENCES projects(id),goal TEXT NOT NULL,revision INTEGER NOT NULL,
          goal_digest TEXT NOT NULL,measurement_revision TEXT NOT NULL,mapping_revision TEXT NOT NULL,
          bundle TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,goal,revision));
        CREATE TABLE IF NOT EXISTS loop_policies(
          project TEXT NOT NULL,goal TEXT NOT NULL,revision INTEGER NOT NULL,policy TEXT NOT NULL,
          issuer TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,goal,revision));
        CREATE TABLE IF NOT EXISTS loop_actions(
          project TEXT NOT NULL,goal TEXT NOT NULL,id TEXT NOT NULL,request_id TEXT NOT NULL,
          goal_revision INTEGER NOT NULL,plan TEXT,receipt TEXT,status TEXT NOT NULL,created REAL NOT NULL,
          PRIMARY KEY(project,id),UNIQUE(project,goal,request_id));
        CREATE TABLE IF NOT EXISTS loop_operations(
          project TEXT NOT NULL,action_id TEXT NOT NULL,operation TEXT NOT NULL,
          status TEXT NOT NULL,receipt TEXT,created REAL NOT NULL,PRIMARY KEY(project,action_id,operation));
        CREATE TABLE IF NOT EXISTS loop_observations(
          project TEXT NOT NULL,goal TEXT NOT NULL,id TEXT NOT NULL,input_sha256 TEXT NOT NULL,
          receipt TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,id));
        CREATE TABLE IF NOT EXISTS loop_answers(
          project TEXT NOT NULL,goal TEXT NOT NULL,id TEXT NOT NULL,input_sha256 TEXT NOT NULL,
          completion_event_id TEXT NOT NULL,measurement_revision TEXT NOT NULL,mapping_revision TEXT NOT NULL,
          verdict TEXT NOT NULL,receipt TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,id));
        CREATE INDEX IF NOT EXISTS loop_answer_completion ON loop_answers(project,goal,completion_event_id);
        ''')

    def __init__(self, app, connect, reader, editor, owner, retention_days, github=None):
        self.connect, self.reader, self.editor, self.owner = connect, reader, editor, owner
        self.retention_days, self.github = retention_days, github
        self.action_locks = defaultdict(threading.Lock)

        @app.get('/api/loop/goals', dependencies=[Depends(reader)])
        def goals(project: str):
            with closing(connect()) as c:
                ids = [row['goal'] for row in c.execute('SELECT DISTINCT goal FROM loop_goals WHERE project=? ORDER BY goal', (project,))]
            views = [self.view(project, goal) for goal in ids]
            return {'goals': [{**view['goal'], **view} for view in views], 'limit': 100}

        @app.put('/api/loop/goals/{goal}/sync', dependencies=[Depends(editor)])
        def sync(project: str, goal: GoalID, req: GoalSource):
            return self.sync(project, goal, req)

        @app.get('/api/loop/goals/{goal}', dependencies=[Depends(reader)])
        def inspect(project: str, goal: GoalID):
            return self.view(project, goal)

        @app.put('/api/loop/goals/{goal}/policy')
        def policy(project: str, goal: GoalID, revision: int, req: LoopPolicy, request: Request):
            return self.set_policy(project, goal, revision, req, request)

        @app.post('/api/loop/goals/{goal}/prepare', dependencies=[Depends(editor)])
        def prepare(project: str, goal: GoalID, req: Prepare):
            return self.prepare(project, goal, str(req.request_id))

        @app.post('/api/loop/actions/{id}/{operation}', dependencies=[Depends(editor)])
        def action(project: str, id: uuid.UUID, operation: Literal['draft-pr', 'reconcile', 'merge'], req: Empty, request: Request):
            return self.act(project, str(id), operation, request)

        @app.put('/api/loop/goals/{goal}/observations/{id}', dependencies=[Depends(reader)])
        def observe(project: str, goal: GoalID, id: uuid.UUID, req: ObservationWindow):
            return self.observe(project, goal, str(id), req)

        @app.put('/api/loop/goals/{goal}/answers/{id}', dependencies=[Depends(editor)])
        def assess(project: str, goal: GoalID, id: uuid.UUID, req: AnswerCheck):
            return self.assess(project, goal, str(id), req)

        @app.put('/api/loop/goals/{goal}/external/{id}', dependencies=[Depends(editor)])
        def external(project: str, goal: GoalID, id: uuid.UUID, req: ExternalObservation | ExternalContractGap):
            return self.external(project, goal, str(id), req)

        @app.post('/api/loop/goals/{goal}/compare', dependencies=[Depends(reader)])
        def comparison(project: str, goal: GoalID, req: Comparison):
            return self.comparison(project, goal, req)

    def provider(self):
        if self.github is None:
            from loop_github import GitHub
            self.github = GitHub.from_env()
        return self.github

    def goal(self, c, project, goal, revision=None):
        sql = 'SELECT * FROM loop_goals WHERE project=? AND goal=?'
        params = [project, goal]
        if revision is not None:
            sql += ' AND revision=?'
            params.append(revision)
        row = c.execute(sql + ' ORDER BY revision DESC LIMIT 1', params).fetchone()
        if row is None:
            raise HTTPException(404, 'goal not found')
        return {**dict(row), 'bundle': json.loads(row['bundle'])}

    def policy(self, c, project, goal):
        row = c.execute('SELECT * FROM loop_policies WHERE project=? AND goal=? ORDER BY revision DESC LIMIT 1', (project, goal)).fetchone()
        return {**json.loads(row['policy']), 'revision': row['revision'], 'issuer': row['issuer']} if row else None

    def refs(self, goal):
        return {'goal_revision': goal['revision'], 'measurement_revision': goal['measurement_revision'],
                'mapping_revision': goal['mapping_revision'], 'goal_digest': goal['goal_digest']}

    def sync(self, project, goal, req):
        snapshot = self.provider().load_goal(req.model_dump(mode='json'))
        if any(snapshot.get(key) != value for key, value in req.model_dump(mode='json').items()):
            raise HTTPException(409, 'provider source does not match the requested committed goal')
        try:
            definition = GoalDefinition.model_validate(snapshot['definition']).model_dump(mode='json', by_alias=True)
        except ValidationError:
            raise HTTPException(422, 'committed goal definition is unsupported') from None
        if definition['id'] != goal or req.path != '.afterspan/goals/' + goal + '.json':
            raise HTTPException(409, 'goal ID does not match its committed definition')
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM loop_goals WHERE project=? AND goal=? ORDER BY revision DESC LIMIT 1', (project, goal)).fetchone()
            if old and json.loads(old['bundle']) == snapshot:
                return self.view(project, goal, c)
            if not old and c.execute('SELECT count(DISTINCT goal) FROM loop_goals WHERE project=?', (project,)).fetchone()[0] >= 100:
                raise HTTPException(422, 'maximum 100 goals per project')
            revision = old['revision'] + 1 if old else 1
            c.execute('INSERT INTO loop_goals VALUES(?,?,?,?,?,?,?,?)',
                      (project, goal, revision, digest(definition['question']), digest(definition['measurement']),
                       digest(definition['mapping']), canonical(snapshot), time.time()))
            return self.view(project, goal, c)

    def set_policy(self, project, goal, revision, req, request):
        self.owner(request, project)
        header = request.headers.get('authorization', '')
        if header.startswith('Bearer as_workspace_'):
            raise HTTPException(403, 'owner account session or operator required to grant loop authority')
        issuer = 'operator'
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            if not header.startswith('Bearer '):
                session = request.cookies.get('as_session', '')
                row = c.execute('''SELECT s.user_id FROM account_sessions s JOIN memberships m ON m.user_id=s.user_id
                    JOIN projects p ON p.workspace=m.workspace WHERE s.key_hash=? AND s.expires>? AND p.id=? AND m.role='owner' ''',
                    (hashlib.sha256(session.encode()).hexdigest(), time.time(), project)).fetchone()
                if not row:
                    raise HTTPException(403, 'owner account session required')
                issuer = row['user_id']
            current = self.goal(c, project, goal)
            old = self.policy(c, project, goal)
            if revision != (old['revision'] if old else 0):
                raise HTTPException(409, 'loop policy changed; reload before saving')
            source, definition = current['bundle'], current['bundle']['definition']
            if (req.repository != source['repository'] or req.base != source['base']
                    or req.environment != definition['mapping']['environment']):
                raise HTTPException(422, 'policy must match the current committed goal source and environment')
            if req.state == 'active' and req.expires_at.timestamp() <= time.time():
                raise HTTPException(422, 'active policy must expire in the future')
            policy = req.model_dump(mode='json')
            policy.update(max_files=1, max_added_lines=1, max_deleted_lines=0,
                          approved_goal_digest=current['goal_digest'], measurement_revision=current['measurement_revision'],
                          mapping_revision=current['mapping_revision'], tracking_sha256=digest(definition['tracking']),
                          source_repository_id=source.get('repository_id'))
            c.execute('INSERT INTO loop_policies VALUES(?,?,?,?,?,?)', (project, goal, revision + 1, canonical(policy), issuer, time.time()))
            return self.view(project, goal, c)

    def action(self, c, project, id):
        row = c.execute('SELECT * FROM loop_actions WHERE project=? AND id=?', (project, id)).fetchone()
        if row is None:
            raise HTTPException(404, 'action not found')
        result = dict(row)
        for key in ['plan', 'receipt']:
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def prepare(self, project, goal, request_id):
        with self.action_locks[(project, 'prepare', goal, request_id)]:
            with closing(self.connect()) as c, c:
                c.execute('BEGIN IMMEDIATE')
                current = self.goal(c, project, goal)
                definition = current['bundle']['definition']
                completion = definition['measurement']['completion']
                if (definition['mapping']['environment'] != 'local' or completion['event'] != 'codex.answer.ready'
                        or any(item['property'] != 'environment' for item in completion.get('filters', []))
                        or not event_matches({'name': 'codex.answer.ready', 'properties': {'environment': 'local'}}, completion)):
                    raise HTTPException(422, 'the supported repair tracks codex.answer.ready in local only')
                old = c.execute('SELECT id,goal_revision FROM loop_actions WHERE project=? AND goal=? AND request_id=?', (project, goal, request_id)).fetchone()
                if old:
                    if old['goal_revision'] != current['revision']:
                        raise HTTPException(409, 'request ID belongs to an older goal revision')
                    action = self.action(c, project, old['id'])
                    if action['plan'] is not None:
                        return action
                    id = old['id']
                else:
                    id = str(uuid.uuid4())
                    c.execute('INSERT INTO loop_actions VALUES(?,?,?,?,?,?,?,?,?)',
                              (project, goal, id, request_id, current['revision'], None, None, 'preparing', time.time()))
            try:
                plan = self.provider().prepare(current['bundle'])
            except HTTPException:
                with closing(self.connect()) as c, c:
                    c.execute("UPDATE loop_actions SET status='prepare_failed' WHERE project=? AND id=?", (project, id))
                raise
            plan = {**plan, 'id': id, 'action_id': id, **self.refs(current)}
            with closing(self.connect()) as c, c:
                c.execute('UPDATE loop_actions SET plan=?,status=? WHERE project=? AND id=?',
                          (canonical(plan), plan['status'], project, id))
                return self.action(c, project, id)

    def guard(self, project, action, operation, request):
        self.editor(request, project)
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            current = self.goal(c, project, action['goal'])
            policy = self.policy(c, project, action['goal'])
            if current['revision'] != action['goal_revision']:
                raise HTTPException(409, 'goal changed; prepare a new action')
            if not policy or policy['state'] != 'active' or datetime.fromisoformat(policy['expires_at']).timestamp() <= time.time():
                raise HTTPException(403, 'current owner policy is inactive or expired')
            if (policy['approved_goal_digest'] != current['goal_digest']
                    or policy['measurement_revision'] != current['measurement_revision']
                    or policy['mapping_revision'] != current['mapping_revision']
                    or policy['tracking_sha256'] != digest(current['bundle']['definition']['tracking'])
                    or policy['source_repository_id'] != current['bundle'].get('repository_id')):
                raise HTTPException(403, 'goal meaning changed; an owner must grant a new policy revision')
            if policy['issuer'] != 'operator':
                valid = c.execute("SELECT 1 FROM memberships m JOIN projects p ON p.workspace=m.workspace WHERE p.id=? AND m.user_id=? AND m.role='owner'", (project, policy['issuer'])).fetchone()
                if not valid:
                    raise HTTPException(403, 'policy issuer is no longer a workspace owner')
            plan = action['plan']
            if (operation not in policy['operations'] or plan['repository'] != policy['repository']
                    or plan['base'] != policy['base'] or current['bundle']['definition']['mapping']['environment'] != policy['environment']):
                raise HTTPException(403, 'current owner policy does not permit this operation')
            files = plan.get('files', [])
            if len(files) != 1 or files[0].get('path') not in policy['paths']:
                raise HTTPException(403, 'plan is outside the owner path and file bounds')
            return {key: value for key, value in policy.items() if key != 'issuer'}

    def act(self, project, id, operation, request):
        lock = self.action_locks[(project, id)]
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'action is running; reconcile after it finishes')
        try:
            return self.run_action(project, id, operation, request)
        finally:
            lock.release()

    def run_action(self, project, id, operation, request):
        with closing(self.connect()) as c:
            action = self.action(c, project, id)
        if not action['plan'] or action['plan']['status'] != 'prepared':
            raise HTTPException(409, 'action has no prepared tracking repair')
        policy = self.guard(project, action, operation, request) if operation != 'reconcile' else None
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            action = self.action(c, project, id)
            old = c.execute('SELECT * FROM loop_operations WHERE project=? AND action_id=? AND operation=?', (project, id, operation)).fetchone()
            if old and old['status'] == 'complete' and operation != 'reconcile':
                return self.action(c, project, id)
            if old and old['status'] == 'pending' and operation != 'reconcile':
                raise HTTPException(409, 'operation is pending; reconcile before retrying')
            if operation != 'reconcile' and action['status'] in {'pending', 'reconcile_required'}:
                raise HTTPException(409, 'provider outcome is unknown; reconcile before another write')
            if operation == 'merge' and not action['receipt']:
                raise HTTPException(409, 'draft provider receipt required before merge')
            c.execute('INSERT INTO loop_operations VALUES(?,?,?,?,?,?) ON CONFLICT(project,action_id,operation) DO UPDATE SET status=excluded.status,receipt=NULL,created=excluded.created',
                      (project, id, operation, 'pending', None, time.time()))
            c.execute("UPDATE loop_actions SET status='pending' WHERE project=? AND id=?", (project, id))
        def guard():
            current = self.guard(project, action, operation, request)
            if current['revision'] != policy['revision']:
                raise HTTPException(409, 'owner policy changed during the action; reconcile before retrying')
            return current
        try:
            provider = self.provider()
            if operation == 'draft-pr':
                receipt = provider.draft(action['plan'], id, guard)
            elif operation == 'merge':
                receipt = provider.merge(action['plan'], action['receipt'], policy, guard)
            else:
                receipt = provider.reconcile(action['plan'], action['receipt'])
        except HTTPException:
            with closing(self.connect()) as c, c:
                c.execute("UPDATE loop_operations SET status='blocked' WHERE project=? AND action_id=? AND operation=?", (project, id, operation))
                c.execute("UPDATE loop_actions SET status='reconcile_required' WHERE project=? AND id=?", (project, id))
            raise
        except Exception:
            receipt = {'status': 'reconcile_required', 'action_id': id, 'reason': 'provider outcome unavailable'}
        status = receipt.get('status', 'reconcile_required')
        with closing(self.connect()) as c, c:
            c.execute('UPDATE loop_operations SET status=?,receipt=? WHERE project=? AND action_id=? AND operation=?',
                      ('uncertain' if status == 'reconcile_required' else 'complete', canonical(receipt), project, id, operation))
            c.execute('UPDATE loop_actions SET status=?,receipt=? WHERE project=? AND id=?', (status, canonical(receipt), project, id))
            if operation == 'reconcile' and status != 'reconcile_required':
                for pending in ['draft-pr', 'merge']:
                    complete = receipt.get('pr_number') if pending == 'draft-pr' else receipt.get('merged') is True
                    c.execute('''UPDATE loop_operations SET status=?,receipt=? WHERE project=? AND action_id=?
                        AND operation=? AND status IN ('pending','uncertain','blocked')''',
                        ('complete' if complete else 'retryable', canonical(receipt), project, id, pending))
            return self.action(c, project, id)

    def existing_receipt(self, c, table, project, goal, id, fingerprint):
        row = c.execute('SELECT goal,input_sha256,receipt FROM ' + table + ' WHERE project=? AND id=?', (project, id)).fetchone()
        if row:
            if row['goal'] != goal or row['input_sha256'] != fingerprint:
                raise HTTPException(409, 'receipt ID already has a different immutable payload')
            return json.loads(row['receipt'])

    def evidence(self, c, project, goal, id):
        row = c.execute('SELECT receipt FROM loop_observations WHERE project=? AND goal=? AND id=?', (project, goal, id)).fetchone()
        if row is None:
            raise HTTPException(404, 'observation receipt not found')
        return json.loads(row['receipt'])

    def observe(self, project, goal, id, req):
        window = req.model_dump(mode='json')
        fingerprint = digest(window)
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            old = self.existing_receipt(c, 'loop_observations', project, goal, id, fingerprint)
            if old:
                return old
            current = self.goal(c, project, goal)
            definition = current['bundle']['definition']
            stamps = {key: getattr(req, key).timestamp() for key in ['entry_start', 'entry_end', 'observed_through', 'as_of']}
            stamps['release'] = req.release
            rows = c.execute('''SELECT id,name,distinct_id,ts,received,properties FROM events
                WHERE project=? AND ts>=? AND ts<=? AND received<=? ORDER BY ts,id LIMIT ?''',
                (project, stamps['entry_start'], min(stamps['observed_through'], stamps['entry_end'] + 86400), stamps['as_of'], EVENT_CAP + 1)).fetchall()
            if len(rows) > EVENT_CAP:
                raise HTTPException(422, 'observation exceeds 200,000 events; shorten the entry window')
            events = [{**dict(row), 'properties': json.loads(row['properties'])} for row in rows]
            assessments = defaultdict(list)
            for row in c.execute('''SELECT completion_event_id,verdict FROM loop_answers WHERE project=? AND goal=?
                    AND measurement_revision=? AND mapping_revision=? AND created<=?''',
                    (project, goal, current['measurement_revision'], current['mapping_revision'], stamps['as_of'])):
                assessments[row['completion_event_id']].append(row['verdict'])
            result = count_first_entry_outcomes(events, definition['measurement'], stamps, assessments, definition['mapping']['environment'])
            retention_gap = stamps['entry_start'] < time.time() - self.retention_days * 86400
            erasure = bool(c.execute('SELECT 1 FROM erased_identities WHERE project=? AND erased>=? LIMIT 1',
                                    (project, stamps['entry_start'])).fetchone())
            limitations = list(LIMITATIONS)
            if retention_gap:
                limitations.append('The entry window extends beyond current event retention; historical coverage is incomplete.')
            if erasure:
                limitations.append('Authorized erasure since this entry window removed retained evidence; historical coverage is incomplete.')
            release_gap = any(result[key] for key in ['release_excluded_starters', 'release_missing_starters',
                                                      'release_mismatched_completions', 'release_missing_completions'])
            if result['release_excluded_starters'] or result['release_missing_starters']:
                limitations.append('Some first starters have a different or missing release label and were excluded. A later retry does not reset their first entry clock.')
            if result['release_mismatched_completions'] or result['release_missing_completions']:
                limitations.append('Some timely completions have a different or missing release label and cannot qualify this release.')
            if req.release is None:
                limitations.append('This observation is not filtered to an exact release and cannot prove a new release outcome.')
            receipt = {'id': id, 'goal_id': goal, **self.refs(current), 'source': 'native',
                       'environment': definition['mapping']['environment'], 'window': window, **result,
                       'coverage': {'complete': not retention_gap and not erasure and not release_gap and result['waiting'] == 0,
                                    'retention_days': self.retention_days, 'retention_gap': retention_gap, 'erasure': erasure,
                                    'release_gap': release_gap,
                                    'event_cap': EVENT_CAP, 'received_cutoff': window['as_of']},
                       'limitations': limitations, 'next_action': next_action(result)}
            c.execute('INSERT INTO loop_observations VALUES(?,?,?,?,?,?)', (project, goal, id, fingerprint, canonical(receipt), time.time()))
            return receipt

    def assess(self, project, goal, id, req):
        payload = req.model_dump(mode='json')
        fingerprint = digest(payload)
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            old = self.existing_receipt(c, 'loop_answers', project, goal, id, fingerprint)
            if old:
                return old
            evidence = self.evidence(c, project, goal, str(req.evidence_id))
            current = self.goal(c, project, goal, evidence['goal_revision'])
            definition = current['bundle']['definition']
            row = c.execute('SELECT * FROM events WHERE project=? AND id=?', (project, req.completion_event_id)).fetchone()
            if row is None:
                raise HTTPException(422, 'completion event not observed')
            event = {**dict(row), 'properties': json.loads(row['properties'])}
            if (not eligible(event, definition['mapping']['environment'])
                    or not event_matches(event, definition['measurement']['completion'])
                    or event['properties'].get('observation_id') != str(req.evidence_id)
                    or evidence['window'] and evidence['window'].get('release') is not None
                    and event['properties'].get('release') != evidence['window']['release']):
                raise HTTPException(422, 'completion does not match the committed goal and evidence receipt')
            answer = req.answer.model_dump(mode='json')
            check = check_answer(answer, evidence)
            stored_answer = {**answer, 'limitations': answer['limitations'] if answer['limitations'] == evidence['limitations'] else []}
            receipt = {'id': id, 'goal_id': goal, 'evidence_id': str(req.evidence_id),
                       'completion_event_id': req.completion_event_id, **self.refs(current),
                       'answer_sha256': digest(answer), 'answer': stored_answer, **check}
            c.execute('INSERT INTO loop_answers VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (project, goal, id, fingerprint, req.completion_event_id, current['measurement_revision'],
                       current['mapping_revision'], check['verdict'], canonical(receipt), time.time()))
            return receipt

    def external(self, project, goal, id, req):
        payload = req.model_dump(mode='json')
        fingerprint = digest(payload)
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            old = self.existing_receipt(c, 'loop_observations', project, goal, id, fingerprint)
            if old:
                return old
            current = self.goal(c, project, goal)
            if isinstance(req, ExternalContractGap):
                receipt = {'id': id, 'goal_id': goal, **self.refs(current), **payload, 'source': 'externally_attested',
                           'provenance': 'caller_attestation_not_provider_verified',
                           'environment': current['bundle']['definition']['mapping']['environment'], 'window': None,
                           **dict.fromkeys(['entrants', 'mature', 'waiting', 'qualified', 'failed', 'unassessed',
                                            'timed_out', 'technical_failures', 'rate', 'confirmed_lower_bound']),
                           'quality': 'unassessed', 'coverage': {'complete': False, 'reason': 'required event contract not observed'},
                           'limitations': ['Required event names were absent from the inspected external taxonomy. This does not prove zero entrants or completions.',
                                           'External source coverage is unavailable; no outcome denominator or rate can be inferred.'],
                           'next_action': 'prepare_tracking'}
                c.execute('INSERT INTO loop_observations VALUES(?,?,?,?,?,?)', (project, goal, id, fingerprint, canonical(receipt), time.time()))
                return receipt
            if req.measurement_revision != current['measurement_revision'] or req.mapping_revision != current['mapping_revision']:
                raise HTTPException(409, 'attestation meaning and mapping must match the synced goal')
            window = {key: payload.pop(key) for key in ObservationWindow.model_fields}
            counts = {key: payload[key] for key in ['entrants', 'mature', 'waiting', 'qualified', 'failed', 'unassessed', 'timed_out', 'technical_failures', 'rate']}
            result = {**counts, 'qualified': 0, 'failed': 0, 'unassessed': req.mature - req.timed_out,
                      'rate': None, 'confirmed_lower_bound': None, 'eligibility': 'externally_attested_starters', 'unit': 'workspace'}
            receipt = {'id': id, 'goal_id': goal, **self.refs(current), 'source': 'externally_attested',
                       'provenance': 'caller_attestation_not_provider_verified', 'source_url': req.source_url,
                       'query_sha256': req.query_sha256, 'environment': current['bundle']['definition']['mapping']['environment'],
                       'window': window, 'attested_counts': counts, **result, 'quality': 'unassessed',
                       'coverage': {'complete': False, 'reason': 'external provenance and coverage are not independently verified'},
                       'limitations': [*LIMITATIONS, 'External counts are attested, kept separate from native evidence and not independently verified.'],
                       'next_action': 'review_answers'}
            c.execute('INSERT INTO loop_observations VALUES(?,?,?,?,?,?)', (project, goal, id, fingerprint, canonical(receipt), time.time()))
            return receipt

    def comparison(self, project, goal, req):
        with closing(self.connect()) as c:
            before = self.evidence(c, project, goal, str(req.before))
            after = self.evidence(c, project, goal, str(req.after))
            release = None
            if req.action_id:
                action = self.action(c, project, str(req.action_id))
                if action['goal'] != goal:
                    raise HTTPException(404, 'action not found for this goal')
                release = verified_release(action, after)
        result = compare(before, after, release)
        result['action_id'] = str(req.action_id) if req.action_id else None
        return result

    def view(self, project, goal, connection=None):
        if connection is None:
            with closing(self.connect()) as c:
                return self.view(project, goal, c)
        c = connection
        current = self.goal(c, project, goal)
        policy = self.policy(c, project, goal)
        actions = [self.action(c, project, row['id']) for row in c.execute('SELECT id FROM loop_actions WHERE project=? AND goal=? ORDER BY created DESC LIMIT 100', (project, goal))]
        observations = [json.loads(row['receipt']) for row in c.execute('SELECT receipt FROM loop_observations WHERE project=? AND goal=? ORDER BY created DESC LIMIT 100', (project, goal))]
        history = [{'goal_revision': row['revision'], 'source': json.loads(row['bundle']), 'measurement_revision': row['measurement_revision'],
                    'mapping_revision': row['mapping_revision'], 'goal_digest': row['goal_digest']} for row in c.execute('SELECT * FROM loop_goals WHERE project=? AND goal=? ORDER BY revision', (project, goal))]
        latest = next((action for action in actions if action['goal_revision'] == current['revision']), None)
        current_observations = [item for item in observations if item['measurement_revision'] == current['measurement_revision']
                                and item['mapping_revision'] == current['mapping_revision']]
        definition = current['bundle']['definition']
        release = verified_release(latest, {**self.refs(current), 'environment': definition['mapping']['environment']}) if latest else None
        bound_observations = [item for item in current_observations if release and item['source'] == 'native'
                              and item['window'].get('release') == release['commit']
                              and datetime.fromisoformat(item['window']['entry_start']) >= datetime.fromisoformat(release['released_at'])]
        completion_observed = any(item['source'] == 'native' and item.get('completion_events_observed') for item in current_observations)
        bound_completion = any(item.get('completion_events_observed') for item in bound_observations)
        receipt = latest['receipt'] or {} if latest else {}
        tracking = 'healthy' if latest and latest['status'] == 'healthy' else 'prepared' if latest and latest['plan'] else 'not_prepared'
        stages = {'tracking': tracking, 'pr': 'observed' if receipt.get('pr_number') else 'not_observed',
                  'merge': 'merged' if receipt.get('merged') is True else 'not_observed',
                  'release': 'released' if release else 'release_pending' if receipt.get('merged') is True else 'not_observed',
                  'signal': 'verified_release_completion_observed' if bound_completion else 'completion_observed_unbound' if completion_observed else 'not_observed',
                  'outcome': 'qualified_for_verified_release' if any(item['qualified'] for item in bound_observations) else
                             'qualified_unbound' if any(item['source'] == 'native' and item['qualified'] for item in current_observations) else 'not_observed'}
        following = ('prepare_tracking' if not latest or not latest['plan'] else 'no_change' if latest['status'] == 'healthy' else
                     'review_pr' if not receipt.get('merged') else 'check_release' if stages['release'] != 'released' else
                     current_observations[0]['next_action'] if current_observations else 'collect_evidence')
        return {'goal': {'id': goal, 'question': definition['question'], 'source': current['bundle'],
                         **self.refs(current), 'definition': definition},
                'history': history, 'policy': {key: value for key, value in policy.items() if key != 'issuer'} if policy else None,
                'actions': actions, 'observations': observations, 'dependency_status': 'committed_snapshot',
                'stages': stages, 'next_action': following, 'limitations': list(LIMITATIONS)}
