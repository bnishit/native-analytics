"""Project validated integrations into one outcome shape without a second event stream."""
from audit import ReviewQuery,request_groups,trace_groups,available

CHECKS={'source-facts-v1':('source_facts',1),'growthbook-catalog-v1':('growthbook_catalog',1),'growthbook-catalog-v2':('growthbook_catalog',2)}
WORKFLOWS={'read':'slack_read_request','write':'slack_write_request','unknown':'slack_request_unknown'}
INTEGRATIONS=[{'contract':'workflow-trace-v1','workflows':'Producer-declared workflow handles','checks':'Versioned criteria declared by the producer and saved goal.'},
    {'contract':'slack-request-v1','workflows':list(WORKFLOWS.values()),
     'checks':[{'name':name,'version':version,'method':'source','phase':'before_delivery','producer_verifier':verifier,'evidence_version':'source-check-v1'} for verifier,(name,version) in CHECKS.items()],
     'limits':['Quality evidence requires a bound real quality operation. Old counts alone remain unknown.','Final delivery requires its explicit reply operation and one matching Slack acceptance child.']}]
BINDINGS=('request_ref','attempt_ref','attempt_number','release_ref','release_sha','environment','request_started_at_ms')

def project_slack(audit,project,key,group,q,chain=None):
    row=chain or audit.chain(project,key,group,q,include_trace_attempts=True)
    _,groups,ordered,labels=trace_groups(group);timing=row['trace'];latest=row['latest_attempt_number']
    issues=[i for i in row['issues'] if i!='comparison_scope_unavailable']+timing['issues'];spans=[]
    raw_for_label={str(labels[ref]):ref for ref in ordered}
    for s in timing['spans']:
        raw=raw_for_label[str(s['operation'])];observations=groups[raw]
        starts=[r for r in observations if r['facts']['span_state']=='started'];ends=[r for r in observations if r['facts']['span_state']!='started']
        spans.append({'span':str(s['operation']),'parent':str(s['parent_operation']) if s['parent_operation'] is not None else None,
            'step':s['step'],'attempt_number':s['attempt_number'],'state':s['state'],'duration_ms':s['duration_ms'],
            'first_seen':starts[0]['ts'] if len(starts)==1 else min(r['ts'] for r in observations),
            'last_seen':ends[0]['ts'] if len(ends)==1 else max(r['ts'] for r in observations),
            'missing_start':len(starts)!=1,'parent_missing':s['parent_missing'],'checks':[],'milestone':None,'disposition':None,
            'model_usage':s.get('model_usage')})
    by_label={s['span']:s for s in spans}
    def operation(ref):return by_label.get(str(labels[ref])) if ref in labels else None
    def bound(observation,ref,step):
        span=operation(ref)
        if not span or span['step']!=step or span['missing_start'] or span['state'] not in {'succeeded','failed'}:return False
        rows=groups[ref]
        if not span['first_seen']<=observation['ts']<=span['last_seen']:return False
        if not all(available(observation['facts'].get(k)) for k in ['request_ref','attempt_ref','attempt_number','release_ref','request_started_at_ms']):return False
        return all(all(r['facts'].get(k)==observation['facts'].get(k) for k in BINDINGS) for r in rows)
    quality=[r for r in group if r['facts']['stage']=='quality' and r['facts'].get('attempt_number')==latest]
    checked=quality[0] if len(quality)==1 else None
    if checked:
        f=checked['facts'];span=operation(f.get('quality_span_ref'))
        definition=CHECKS.get(f.get('check_version'));phase={'before_reply':'before_delivery','after_reply':'after_delivery'}.get(f.get('check_phase'))
        if span and definition and phase:
            proof=all(f.get(k) is not None for k in ['quality_span_ref','check_evidence_digest','check_evidence_version'])
            evidence='missing' if not proof else 'resolved' if f['check_evidence_version']=='source-check-v1' and phase=='before_delivery' and span['state']=='succeeded' and bound(checked,f['quality_span_ref'],'quality') else 'mismatch'
            span['checks']=[{'name':definition[0],'version':definition[1],'method':'source','phase':phase,'required':True,
                'result':{'succeeded':'passed','failed':'failed'}.get(f.get('reported_outcome'),'unknown'),'evidence_present':proof,'evidence_status':evidence}]
            span['disposition']={'withheld':'withheld','clarification':'clarification','partial':'partial'}.get(f.get('answer_disposition'))
    replies=[r for r in group if r['facts']['stage']=='reply' and r['facts'].get('attempt_number')==latest]
    if len(replies)==1 and replies[0]['facts'].get('reply_span_ref'):
        reply=replies[0];ref=reply['facts']['reply_span_ref'];parent=operation(ref)
        children=[s for s in spans if s['parent']==str(labels.get(ref)) and s['step']=='slack_delivery' and s['attempt_number']==latest]
        if not parent or not bound(reply,ref,'reply'):issues.append('final_reply_operation_unbound')
        elif len(children)!=1:issues.append('final_delivery_missing_or_ambiguous')
        else:
            child=children[0];raw=raw_for_label[child['span']]
            state=reply['facts'].get('reported_outcome')
            if child['state']!=state or parent['state']!=state or child['missing_start'] or not parent['first_seen']<=child['first_seen']<=child['last_seen']<=reply['ts']:
                issues.append('final_delivery_evidence_conflict')
            elif any(any(r['facts'].get(k)!=reply['facts'].get(k) for k in BINDINGS) for r in groups[raw]):issues.append('final_delivery_scope_conflict')
            elif next(s for s in row['stages'] if s['stage']=='reply')['required'] is not True:issues.append('final_reply_requirement_unknown')
            else:
                child['milestone']='delivery';child['disposition']={'verified':'delivered','withheld':'withheld','clarification':'clarification','partial':'partial'}.get(checked['facts'].get('answer_disposition')) if checked else None
    completion=timing.get('completion',{}).get('processing',{})
    totals=[s for s in spans if s['step']=='total' and s['attempt_number']==latest]
    if totals and completion.get('state') in {'succeeded','failed'}:
        last=max(s['first_seen'] for s in totals);selected=[s for s in totals if s['first_seen']==last]
        if len(selected)==1 and selected[0]['state']==completion['state']:selected[0]['milestone']='completion'
    for stage in row['stages']:
        if stage['required'] is True and stage['state']!='observed':issues.append('required_stage_unobserved:'+stage['stage'])
        if stage['stage'] in {'confirmation','tool'} and stage['required'] not in (True,False):issues.append('stage_requirement_unknown:'+stage['stage'])
        if stage['stage'] in {'parser','planner','tool'} and stage['reported_outcome']=='failed':issues.append('required_stage_failed:'+stage['stage'])
    if row.get('missing_attempt_numbers'):issues.append('missing_attempts')
    clocks={r['facts'].get('request_started_at_ms') for r in group};clock=next(iter(clocks)) if len(clocks)==1 else None
    if len(clocks)>1 or clock is not None and any(r['ts']*1000<clock for r in group):issues.append('request_clock_conflict')
    traffic={r['facts'].get('traffic_kind','unknown') for r in group}
    if len(traffic)>1:issues.append('traffic_kind_conflict')
    releases={r['facts'].get('release_sha') for r in group}
    if len(releases)>1:issues.append('release_conflict')
    # Parent cycles and cross-attempt links cannot qualify an outcome even if timings close.
    parents={s['span']:s['parent'] for s in spans};visited=set()
    for s in spans:
        if s['parent'] in by_label and by_label[s['parent']]['attempt_number']!=s['attempt_number']:issues.append('parent_attempt_conflict')
        seen=set();node=s['span']
        while node in parents and node not in visited:
            if node in seen:issues.append('parent_cycle');break
            seen.add(node);node=parents[node]
        visited.update(seen)
    return {'trace':row['request'],'workflow':WORKFLOWS[row['request_kind']],'environment':row['environment'],
        'traffic_kind':next(iter(traffic)) if len(traffic)==1 else 'unknown','request_started_at_ms':clock,
        'release_sha':next(iter(releases)) if len(releases)==1 else None,'latest_attempt':latest,
        'observed_attempts':row['business_attempts_observed'],'last_seen':max(r['ts'] for r in group),'spans':spans,
        'checks':[{**c,'span':s['span'],'attempt_number':s['attempt_number']} for s in spans if s['attempt_number']==latest for c in s['checks']],
        'issues':sorted(set(issues))}

def slack_outcomes(audit,project,req):
    q=ReviewQuery(**{k:getattr(req,k) for k in ['days','start','end','as_of']},expected_environment=req.environment)
    q,observations,coverage,key=audit.load(project,q);groups,uncorrelated=request_groups(observations)
    traces=[];unassigned=0;other=0
    for group in groups.values():
        row=audit.chain(project,key,group,q,include_trace_attempts=True)
        if row['environment']=='conflict':unassigned+=1;continue
        if row['environment']!=req.environment:other+=1;continue
        trace=project_slack(audit,project,key,group,q,row);traces.append(trace)
        unassigned+=trace['workflow']=='slack_request_unknown'
    result=audit.metadata(project,req,'slack-outcome-adapter-v1')
    result['query']['contract']='slack-request-v1'
    return {**result,'traces':traces,'coverage':{**coverage,'invalid_evidence_receipts':0,'population_unassigned':unassigned,'other_environment_requests':other,
        'uncorrelated_observations':sum(uncorrelated.values())},
        'limits':result['limits']+['Slack request observations are normalized in place; no second task stream or historical evidence is manufactured.',
            'Resolved source-check-v1 evidence binds the immutable producer observation to its real pre-reply quality span and scope. It does not verify the producer HMAC or fetch the external source independently.',
            'Only the explicit final reply operation and its one matching Slack delivery child can establish reported delivery; interim posts cannot substitute.']}
