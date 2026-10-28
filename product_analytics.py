"""Saved cohorts and ordered, identity-based funnels for the open-source core."""
import hashlib
from itertools import groupby
import json
import math
import statistics
import time
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import datetime,timezone
from typing import Literal
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator

PROPERTY=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$'
class Model(BaseModel):model_config=ConfigDict(extra='forbid')

class Window(Model):
    days:int=Field(default=7,ge=1,le=90)
    start:datetime|None=None
    end:datetime|None=None
    as_of:datetime|None=None

    @field_validator('start','end','as_of')
    @classmethod
    def aware_time(cls,value):
        if value is not None and value.tzinfo is None:raise ValueError('use a timestamp with a timezone')
        return value.astimezone(timezone.utc) if value else value

    @model_validator(mode='after')
    def valid_window(self):
        if (self.start is None)!=(self.end is None):raise ValueError('provide both start and end')
        if self.start is not None and not 0<(self.end-self.start).total_seconds()<=90*86400:
            raise ValueError('window must be positive and at most 90 days')
        return self


class PropertyFilter(Model):
    property:str=Field(pattern=PROPERTY)
    value:str|int|float|bool|None

    @field_validator('value')
    @classmethod
    def bounded_value(cls,value):
        if isinstance(value,str) and len(value)>2000:raise ValueError('filter value exceeds 2000 characters')
        if isinstance(value,float) and not math.isfinite(value):raise ValueError('finite numbers only')
        return value


class EventFilter(PropertyFilter):
    op:Literal['eq','in']='eq'
    value:str|int|float|bool|None|list[str|int|float|bool|None]
    @model_validator(mode='after')
    def options(self):
        if self.op=='in':
            if not isinstance(self.value,list) or not 1<=len(self.value)<=20:raise ValueError('in needs 1-20 typed values')
            for value in self.value:PropertyFilter(property=self.property,value=value)
        elif isinstance(self.value,list):raise ValueError('equality needs a scalar value')
        return self

class EventMatch(Model):
    event:str=Field(min_length=1,max_length=160)
    filters:list[EventFilter]=Field(default_factory=list,max_length=10)

class Behavior(EventMatch):
    kind:Literal['event']='event'
    min_count:int=Field(default=1,ge=0,le=200000,strict=True)
    max_count:int|None=Field(default=None,ge=0,le=200000,strict=True)
    @model_validator(mode='after')
    def counts(self):
        if self.max_count is not None and self.max_count<self.min_count:raise ValueError('max_count must be at least min_count')
        return self

class Trait(PropertyFilter):
    kind:Literal['trait']='trait'
    op:Literal['eq','neq','exists','gte','lte']='eq'
    @model_validator(mode='after')
    def numeric(self):
        if self.op in {'gte','lte'} and (not isinstance(self.value,(int,float)) or isinstance(self.value,bool)):raise ValueError('numeric comparison needs a number')
        return self

class SegmentDefinition(Model):
    match:Literal['all','any']='all'
    rules:list[Behavior|Trait]=Field(min_length=1,max_length=12)

class FunnelDefinition(Model):
    steps:list[EventMatch]=Field(min_length=2,max_length=8)
    conversion_seconds:int=Field(default=86400,ge=1,le=30*86400,strict=True)
    segment_id:uuid.UUID|None=None
    breakdown:str|None=Field(default=None,pattern=PROPERTY)

class SegmentQuery(Window):definition:SegmentDefinition
class FunnelQuery(Window):definition:FunnelDefinition
class SavedSegment(Model):
    title:str=Field(min_length=1,max_length=100)
    definition:SegmentDefinition
class SavedFunnel(Model):
    title:str=Field(min_length=1,max_length=100)
    definition:FunnelDefinition

def equal(a,b):
    if isinstance(a,bool) or isinstance(b,bool):return type(a) is type(b) and a==b
    return a==b and (isinstance(a,(int,float))==isinstance(b,(int,float)))

def matches(properties,filters):return all(f.property in properties and (any(equal(properties[f.property],v) for v in f.value) if getattr(f,'op','eq')=='in' else equal(properties[f.property],f.value)) for f in filters)

def event_matches(row,rule):return row['name']==rule.event and matches(row['properties'],rule.filters)

class ProductAnalytics:
    @staticmethod
    def init_db(c):
        c.executescript('''CREATE TABLE IF NOT EXISTS product_definitions(
          project TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,kind TEXT NOT NULL,
          title TEXT NOT NULL,definition TEXT NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL,
          PRIMARY KEY(project,id));
          CREATE INDEX IF NOT EXISTS product_definition_kind ON product_definitions(project,kind);''')

    def __init__(self,app,connect,reader,editor,freeze,metadata):
        self.connect=connect;self.freeze=freeze;self.metadata=metadata
        @app.get('/api/segments',dependencies=[Depends(reader)])
        def segments(project:str):return self.list(project,'segments')
        @app.get('/api/funnels',dependencies=[Depends(reader)])
        def funnels(project:str):return self.list(project,'funnels')
        @app.post('/api/segments',dependencies=[Depends(editor)],status_code=201)
        def save_segment(project:str,req:SavedSegment):return self.save(project,'segments',req)
        @app.post('/api/funnels',dependencies=[Depends(editor)],status_code=201)
        def save_funnel(project:str,req:SavedFunnel):return self.save(project,'funnels',req)
        @app.put('/api/segments/{id}',dependencies=[Depends(editor)])
        def update_segment(project:str,id:uuid.UUID,revision:int,req:SavedSegment):return self.save(project,'segments',req,str(id),revision)
        @app.put('/api/funnels/{id}',dependencies=[Depends(editor)])
        def update_funnel(project:str,id:uuid.UUID,revision:int,req:SavedFunnel):return self.save(project,'funnels',req,str(id),revision)
        @app.delete('/api/segments/{id}',dependencies=[Depends(editor)])
        def delete_segment(project:str,id:uuid.UUID,revision:int):return self.delete(project,'segments',str(id),revision)
        @app.delete('/api/funnels/{id}',dependencies=[Depends(editor)])
        def delete_funnel(project:str,id:uuid.UUID,revision:int):return self.delete(project,'funnels',str(id),revision)
        @app.post('/api/segments/analyze',dependencies=[Depends(reader)])
        def segment_analysis(project:str,req:SegmentQuery):return self.analyze(project,req,'segments')
        @app.post('/api/funnels/analyze',dependencies=[Depends(reader)])
        def funnel_analysis(project:str,req:FunnelQuery):return self.analyze(project,req,'funnels')

    def get(self,c,project,kind,id):
        row=c.execute('SELECT * FROM product_definitions WHERE project=? AND kind=? AND id=?',(project,kind,id)).fetchone()
        if not row:raise HTTPException(404,'saved definition not found in this project')
        return {**dict(row),'definition':json.loads(row['definition'])}
    def list(self,project,kind):
        with closing(self.connect()) as c:rows=c.execute('SELECT * FROM product_definitions WHERE project=? AND kind=? ORDER BY updated DESC,id',(project,kind)).fetchall()
        return {kind:[{**dict(r),'definition':json.loads(r['definition'])} for r in rows]}
    def save(self,project,kind,req,id=None,revision=None):
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            if kind=='funnels' and req.definition.segment_id:self.get(c,project,'segments',str(req.definition.segment_id))
            if id:
                row=self.get(c,project,kind,id)
                if revision!=row['revision']:raise HTTPException(409,'definition changed; reload before saving')
            elif c.execute('SELECT count(*) FROM product_definitions WHERE project=? AND kind=?',(project,kind)).fetchone()[0]>=100:
                raise HTTPException(422,'maximum 100 saved definitions per kind and project')
            id=id or str(uuid.uuid4());revision=(revision or 0)+1
            c.execute('INSERT INTO product_definitions VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,id) DO UPDATE SET title=excluded.title,definition=excluded.definition,revision=excluded.revision,updated=excluded.updated',
                (project,id,kind,req.title,req.definition.model_dump_json(),revision,time.time()))
            return self.get(c,project,kind,id)
    def delete(self,project,kind,id,revision):
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE');row=self.get(c,project,kind,id)
            if revision!=row['revision']:raise HTTPException(409,'definition changed; reload before removing')
            if kind=='segments' and c.execute("SELECT 1 FROM product_definitions WHERE project=? AND kind='funnels' AND json_extract(definition,'$.segment_id')=?",(project,id)).fetchone():
                raise HTTPException(409,'segment is used by a saved funnel; edit that funnel first')
            c.execute('DELETE FROM product_definitions WHERE project=? AND id=?',(project,id))
        return {'deleted':True}
    def load(self,c,project,q):
        rows=c.execute("SELECT name,distinct_id,ts,properties FROM events WHERE project=? AND ts>=? AND ts<? AND received<=? AND name NOT IN ('analytics.pipeline','afterspan.setup.checked','trace.observed') AND json_type(properties,'$.afterspan_trace') IS NULL ORDER BY ts,id LIMIT 200001",(project,q.start.timestamp(),q.end.timestamp(),q.as_of.timestamp())).fetchall()
        if len(rows)>200000:raise HTTPException(422,'analysis exceeds 200,000 events; shorten the window')
        return [{**dict(r),'properties':json.loads(r['properties'])} for r in rows]
    def cohort(self,c,project,rows,definition):
        by_id=defaultdict(list)
        for row in rows:
            if row['distinct_id']:by_id[row['distinct_id']].append(row)
        traits={}
        if any(isinstance(r,Trait) for r in definition.rules):
            identities=list(by_id)
            for offset in range(0,len(identities),500):
                batch=identities[offset:offset+500]
                traits.update({r['distinct_id']:json.loads(r['attributes']) for r in c.execute('SELECT distinct_id,attributes FROM people WHERE project=? AND distinct_id IN ('+','.join('?' for _ in batch)+')',[project,*batch])})
        members=set()
        for identity,events in by_id.items():
            results=[]
            for rule in definition.rules:
                if isinstance(rule,Behavior):
                    n=sum(event_matches(e,rule) for e in events)
                    results.append(n>=rule.min_count and (rule.max_count is None or n<=rule.max_count))
                else:
                    p=traits.get(identity,{});exists=rule.property in p;value=p.get(rule.property)
                    if rule.op=='exists':result=exists
                    elif rule.op=='eq':result=exists and equal(value,rule.value)
                    elif rule.op=='neq':result=exists and not equal(value,rule.value)
                    else:result=exists and isinstance(value,(int,float)) and not isinstance(value,bool) and (value>=rule.value if rule.op=='gte' else value<=rule.value)
                    results.append(result)
            if (all(results) if definition.match=='all' else any(results)):members.add(identity)
        return members,len(by_id)
    def analyze(self,project,req,kind):
        q=self.freeze(req);definition=q.definition
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            rows=self.load(c,project,q);segment=None
            if kind=='segments':members,active=self.cohort(c,project,rows,definition)
            elif definition.segment_id:
                segment=self.get(c,project,'segments',str(definition.segment_id))
                members,active=self.cohort(c,project,rows,SegmentDefinition.model_validate(segment['definition']))
            else:members={r['distinct_id'] for r in rows if r['distinct_id']};active=len(members)
        result={'matched_identities':len(members),'active_identities':active} if kind=='segments' else self.funnel(rows,members,definition,q.end.timestamp())
        evidence=self.metadata(project,q,'product-v2')
        evidence['fingerprint']=hashlib.sha256(json.dumps({'fingerprint':evidence['fingerprint'],'segment':segment},sort_keys=True).encode()).hexdigest()
        return {**result,**evidence,'segment':segment,'anonymous_events_excluded':sum(not r['distinct_id'] for r in rows),
          'matched_events_scanned':len(rows),'unit':'distinct_id','population':'Identities with at least one retained product event in this window.',
          'traits':'Trait filters use current traits, even for frozen windows. Event rules use the returned event window and receipt cutoff. Segment membership is checked over the whole window, not at funnel entry time.',
          'semantics':'Strict timestamp order; one identity per step; best sequence across repeated starts; all steps inside the selected window and conversion time from start. Equal timestamps cannot advance steps. Missing identities are excluded.' if kind=='funnels' else 'Dynamic membership; all/any rules apply to active identities. Zero-event rules cannot include identities with no activity in this window.'}
    def funnel(self,rows,members,definition,window_end=None):
        steps=definition.steps;states={};reached=[set() for _ in steps];elapsed=[]
        for t,group in groupby(rows,key=lambda r:r['ts']):
            previous={}
            for row in group:
                identity=row['distinct_id']
                if identity not in members:continue
                state=states.setdefault(identity,[None]*len(steps))
                old=previous.setdefault(identity,state.copy())
                for i,step in enumerate(steps):
                    if not event_matches(row,step):continue
                    if i==0:state[0]=(t,t);reached[0].add(identity)
                    elif old[i-1] and t-old[i-1][0]<=definition.conversion_seconds:
                        candidate=(old[i-1][0],t)
                        if state[i] is None or candidate[0]>state[i][0]:state[i]=candidate
                        if i==len(steps)-1 and identity not in reached[i]:elapsed.append(t-candidate[0])
                        reached[i].add(identity)
        counts=[len(s) for s in reached];total=counts[0]
        breakdown=[]
        if definition.breakdown:
            groups={};seen=set()
            for row in rows:
                identity=row['distinct_id']
                if identity not in reached[0] or identity in seen or not event_matches(row,steps[0]):continue
                seen.add(identity);missing=definition.breakdown not in row['properties'];value=row['properties'].get(definition.breakdown)
                if isinstance(value,(list,dict)):raise HTTPException(422,'funnel breakdown needs a scalar property')
                label=('missing' if missing else type(value).__name__)+':'+json.dumps(value)
                groups.setdefault(label,{'value':value,'missing':missing,'members':set()})['members'].add(identity)
            if len(groups)>20:raise HTTPException(422,'breakdown exceeds 20 values; add filters rather than truncate cohorts')
            for group in groups.values():
                result=self.funnel(rows,group['members'],definition.model_copy(update={'breakdown':None}),window_end)
                breakdown.append({'value':group['value'],'missing':group['missing'],'entrants':result['entrants'],'converted':result['converted'],'conversion_rate':result['conversion_rate'],'step_counts':[s['identities'] for s in result['steps']]})
        return {'open_window_nonconverters':sum(identity not in reached[-1] and states[identity][0][0]+definition.conversion_seconds>window_end for identity in reached[0]) if window_end is not None else None,'breakdown':breakdown,'rate_definition':'Unique converters divided by unique entrants in the selected window; not an average of daily conversion rates.','breakdown_assignment':'First qualifying entry event per identity; disjoint groups, no automatic good/bad interpretation.','entrants':total,'converted':counts[-1],'conversion_rate':counts[-1]/total if total else None,
          'median_conversion_seconds':statistics.median(elapsed) if elapsed else None,
          'steps':[{'event':s.event,'identities':counts[i],'conversion_rate':counts[i]/total if total else None,
                    'drop_off':counts[i-1]-counts[i] if i else 0,'step_rate':counts[i]/counts[i-1] if i and counts[i-1] else (1 if not i and total else None)} for i,s in enumerate(steps)]}
