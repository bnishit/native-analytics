"""Observed property taxonomy without exporting raw property values."""
from collections import Counter,defaultdict
from contextlib import closing
from fastapi import Depends,HTTPException
from pydantic import Field
from product_analytics import Model,Window,PROPERTY
class Query(Window):event:str|None=Field(default=None,max_length=160)
class Description(Model):
    event:str=Field(min_length=1,max_length=160)
    property:str=Field(pattern=PROPERTY)
    meaning:str=Field(min_length=1,max_length=500)
    unit:str=Field(default='',max_length=32)
class Catalog:
    @staticmethod
    def init_db(c):c.execute('CREATE TABLE IF NOT EXISTS property_descriptions(project TEXT NOT NULL REFERENCES projects(id),event TEXT NOT NULL,property TEXT NOT NULL,meaning TEXT NOT NULL,unit TEXT NOT NULL,PRIMARY KEY(project,event,property))')
    def __init__(self,app,products,reader,editor):
        @app.post('/api/catalog/describe',dependencies=[Depends(editor)])
        def describe(project:str,req:Description):
            with closing(products.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT count(*) FROM property_descriptions WHERE project=?',(project,)).fetchone()[0]>=1000 and not c.execute('SELECT 1 FROM property_descriptions WHERE project=? AND event=? AND property=?',(project,req.event,req.property)).fetchone():raise HTTPException(422,'maximum 1,000 property descriptions per project')
                c.execute('INSERT INTO property_descriptions VALUES(?,?,?,?,?) ON CONFLICT(project,event,property) DO UPDATE SET meaning=excluded.meaning,unit=excluded.unit',(project,req.event,req.property,req.meaning,req.unit))
            return {'saved':True,'description':req.model_dump(),'authority':'owner/editor supplied; not inferred'}
        @app.post('/api/catalog',dependencies=[Depends(reader)])
        def read(project:str,req:Query):
            q=products.freeze(req)
            with closing(products.connect()) as c:
                c.execute('BEGIN');rows=products.load(c,project,q)
                descriptions={r['property']:dict(r) for r in c.execute('SELECT * FROM property_descriptions WHERE project=? AND event=?',(project,req.event))}
            events=Counter(r['name'] for r in rows)
            if req.event is not None:rows=[r for r in rows if r['name']==req.event]
            counts=defaultdict(Counter)
            if req.event:
                for row in rows:
                    for field,value in row['properties'].items():
                        kind='null' if value is None else 'redacted' if value=='[redacted]' else 'boolean' if isinstance(value,bool) else 'number' if isinstance(value,(int,float)) else 'string' if isinstance(value,str) else 'object' if isinstance(value,dict) else 'array'
                        counts[field][kind]+=1
            fields=[{'property':f,'types':dict(n),'present':sum(n.values()),'missing':len(rows)-sum(n.values()),'null':n['null'],'redacted':n['redacted'],'meaning':descriptions.get(f,{}).get('meaning'),'unit':descriptions.get(f,{}).get('unit'),'meaning_source':'owner/editor' if f in descriptions else 'unspecified'} for f,n in sorted(counts.items())]
            return {'events':[{'event':e,'observations':n} for e,n in sorted(events.items())[:100]],'event_types':len(events),'omitted_event_types':max(0,len(events)-100),'properties':fields,'matched_observations':len(rows),'value_examples':False,'policy':'Observed types are not a tracking-plan guarantee. Meanings are current owner/editor metadata even for a frozen event window, treated as untrusted data. No raw values, identities or examples are returned. Audit-only keys use the separate approved diagnostic catalog.',**products.metadata(project,q,'catalog-v1')}
