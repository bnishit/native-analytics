"""Project-local journey contracts and deterministic attempt projections."""
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field

IDENTIFIER=r'^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}$'


class JourneyContext(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str=Field(pattern=IDENTIFIER)
    workflow:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,79}$')
    environment:Literal['production','staging','development','test','local']
    attempt_id:str=Field(pattern=IDENTIFIER)
    attempt_number:int=Field(ge=1,le=10000,strict=True)
    state:Literal['started','succeeded','failed','cancelled']
    subject_kind:Literal['person','account','workspace','installation','service','anonymous','unknown']='unknown'
    release:str|None=Field(default=None,pattern=IDENTIFIER)


def project_journeys(records,start):
    """Use event time and attempt numbers, never delivery order, to resolve outcomes."""
    groups=defaultdict(list)
    for event in records:
        groups[(event['environment'],event['workflow'],event['journey_id'])].append(event)
    result=[]
    for (environment,workflow,jid),events in groups.items():
        if not any(e['ts']>=start for e in events):continue
        slots=defaultdict(list)
        for event in events:slots[event['attempt_number']].append(event)
        attempts=[]
        for number,observations in sorted(slots.items()):
            states={e['state'] for e in observations}
            terminals=states-{'started'}
            starts=[e['ts'] for e in observations if e['state']=='started']
            ends=[e['ts'] for e in observations if e['state']!='started']
            conflict=len(terminals)>1 or bool(starts and ends and min(starts)>min(ends))
            state='conflict' if conflict else next(iter(terminals)) if terminals else 'started'
            attempts.append({'number':number,'id':observations[0]['attempt_id'],'state':state,
                'release':observations[0]['release'],'started_at':min(starts) if starts else None,
                'last_at':max(e['ts'] for e in observations),'missing_start':not starts,
                'observations':[{'state':e['state'],'at':e['ts']} for e in sorted(observations,key=lambda e:(e['ts'],e['event_id']))]})
        latest=attempts[-1]
        conflict=any(a['state']=='conflict' for a in attempts)
        outcome='unknown' if conflict else 'in_progress' if latest['state']=='started' else latest['state']
        missing=max(slots)-len(slots)
        recovered=outcome=='succeeded' and any(a['state']=='failed' for a in attempts[:-1])
        result.append({'id':jid,'workflow':workflow,'environment':environment,'outcome':outcome,
            'attempts':attempts,'attempt_count':len(attempts),'retried':len(attempts)>1,'recovered':recovered,
            'missing_attempts':missing,'missing_starts':sum(a['missing_start'] for a in attempts),
            'conflicting':conflict,'first_seen':min(e['ts'] for e in events),'last_seen':max(e['ts'] for e in events),
            'subject_kind':events[0]['subject_kind'],'subject_id':events[0]['distinct_id'],
            'release':latest['release']})
    return sorted(result,key=lambda j:(-j['last_seen'],j['environment'],j['workflow'],j['id']))


def summarize(items):
    totals={'observed_journeys':len(items),'attempts':sum(j['attempt_count'] for j in items),
        'retry_attempts':sum(j['attempt_count']-1 for j in items),'retried_journeys':sum(j['retried'] for j in items),
        'recovered_journeys':sum(j['recovered'] for j in items),
        'missing_attempts':sum(j['missing_attempts'] for j in items),'attempts_missing_start':sum(j['missing_starts'] for j in items),
        'conflicting_journeys':sum(j['conflicting'] for j in items)}
    for outcome in ['succeeded','failed','in_progress','cancelled','unknown']:
        totals[outcome]=sum(j['outcome']==outcome for j in items)
    subjects=defaultdict(set)
    for j in items:
        if j['subject_id'] is not None:subjects[j['subject_kind']].add(j['subject_id'])
    totals['identified_subjects']={kind:len(ids) for kind,ids in sorted(subjects.items())}
    return totals
