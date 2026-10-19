"""Bounded calendar retention with explicit immature cells and cohort denominators."""
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from fastapi import Depends
from pydantic import Field,field_validator
from product_analytics import Model,Window,EventMatch,event_matches
from typing import Literal
class Definition(Model):
    start:EventMatch
    return_event:EventMatch
    period:Literal['day','week']='week'
    periods:int=Field(default=12,ge=1,le=12,strict=True)
    method:Literal['exact','on_or_after']='exact'
    timezone:str=Field(default='UTC',max_length=64)
    @field_validator('timezone')
    @classmethod
    def zone(cls,value):
        try:ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError):raise ValueError('use an IANA timezone') from None
        return value
class Query(Window):definition:Definition
class Saved(Model):
    title:str=Field(min_length=1,max_length=100)
    definition:Definition
class Retention:
    def __init__(self,app,products,reader,editor):
        self.products=products
        @app.get('/api/retention',dependencies=[Depends(reader)])
        def saved(project:str):return products.list(project,'retention')
        @app.post('/api/retention',dependencies=[Depends(editor)],status_code=201)
        def create(project:str,req:Saved):return products.save(project,'retention',req)
        @app.put('/api/retention/{id}',dependencies=[Depends(editor)])
        def update(project:str,id:uuid.UUID,revision:int,req:Saved):return products.save(project,'retention',req,str(id),revision)
        @app.delete('/api/retention/{id}',dependencies=[Depends(editor)])
        def remove(project:str,id:uuid.UUID,revision:int):return products.delete(project,'retention',str(id),revision)
        @app.post('/api/retention/analyze',dependencies=[Depends(reader)])
        def analyze(project:str,req:Query):return self.analyze(project,req)
    def analyze(self,project,req):
        q=self.products.freeze(req);d=q.definition;zone=ZoneInfo(d.timezone);size=7 if d.period=='week' else 1
        with closing(self.products.connect()) as c:
            c.execute('BEGIN');rows=self.products.load(c,project,q)
        starts={};returns=defaultdict(list)
        for r in rows:
            identity=r['distinct_id']
            if not identity:continue
            if event_matches(r,d.start):starts.setdefault(identity,r['ts'])
            if event_matches(r,d.return_event):returns[identity].append(r['ts'])
        groups=defaultdict(list)
        for identity,ts in starts.items():
            local=datetime.fromtimestamp(ts,zone);bucket=local.replace(hour=0,minute=0,second=0,microsecond=0)
            if size==7:bucket-=timedelta(days=bucket.weekday())
            groups[bucket].append((identity,ts))
        cohorts=[];overall=[{'period':i,'eligible':0,'retained':0} for i in range(d.periods+1)]
        for bucket,identities in sorted(groups.items()):
            cells=[]
            for i in range(d.periods+1):
                mature=(bucket+timedelta(days=(i+1)*size)).timestamp()<=q.end.timestamp()
                returned=0
                if i==0:returned=len(identities)
                else:
                    for identity,first in identities:
                        offsets=[(datetime.fromtimestamp(t,zone).date()-bucket.date()).days//size for t in returns[identity] if t>first]
                        returned+=any(offset==i if d.method=='exact' else offset>=i for offset in offsets)
                eligible=len(identities) if mature else 0
                cell={'period':i,'mature':mature,'eligible':eligible,'retained':returned if mature else None,'rate':returned/eligible if eligible else None}
                cells.append(cell)
                if mature:overall[i]['eligible']+=eligible;overall[i]['retained']+=returned
            cohorts.append({'start':bucket.isoformat(),'identities':len(identities),'partial_entry_bucket':bucket.timestamp()<q.start.timestamp(),'cells':cells})
        for cell in overall:cell['rate']=cell['retained']/cell['eligible'] if cell['eligible'] else None
        return {'cohorts':cohorts,'overall':overall,'started_identities':len(starts),'anonymous_events_excluded':sum(not r['distinct_id'] for r in rows),'unit':'distinct_id','timezone':d.timezone,
          'semantics':'First qualifying start per identity inside this selected window, not lifetime-new users. Day/week buckets use the selected timezone; weeks start Monday. Period 0 is cohort entry. Exact means return in that calendar bucket; on_or_after means return in that bucket or later, before the window end. Immature buckets return null, never zero. Partial entry buckets are marked. Overall rates include only mature cohorts; do not sum rates or overlapping uniques.',
          **self.products.metadata(project,q,'retention-v1')}
