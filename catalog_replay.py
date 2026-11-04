"""Content-free catalog check fixtures; never a full agent-request replay."""
from typing import Annotated,Literal
from pydantic import Field,StrictBool
from product_analytics import Model

Count=Annotated[int,Field(ge=0,le=10**9,strict=True)]
Alias=Annotated[int,Field(ge=1,le=100,strict=True)]
Entries=Annotated[int,Field(ge=0,le=1000,strict=True)]
ReplayReason=Literal['request_unavailable','candidate_unavailable','source_unavailable','source_unsupported','projection_limit','release_unavailable','binding_rejected']

class CatalogRequest(Model):
    environment:Literal['production','staging']
    status:Literal['enabled','disabled','all']
    limit:Annotated[int,Field(ge=1,le=1000,strict=True)]
    intent:Literal['bounded','full','total']

class Coverage(Model):
    checked:Count
    complete:StrictBool

class Claim(Model):
    row:Alias
    enabled:StrictBool
    entries:Entries|None
    version:Alias|None

class Candidate(Model):
    environment:Literal['production','staging']
    request:CatalogRequest|None
    coverage:Coverage|None
    matching:Count|None
    claims:list[Claim]=Field(max_length=15)

class SourceRow(Model):
    row:Alias
    enabled:StrictBool|None
    archived:StrictBool
    entries:Entries|None
    version:Alias|None

class Source(Model):
    coverage:Coverage
    rows:list[SourceRow]=Field(max_length=50)

class Quality(Model):
    check_version:Literal['growthbook-catalog-v1','growthbook-catalog-v2']
    reported_outcome:Literal['succeeded','failed','unknown']
    checked_claims:Annotated[int,Field(ge=0,le=16,strict=True)]
    mismatched_claims:Annotated[int,Field(ge=0,le=16,strict=True)]
    unknown_claims:Annotated[int,Field(ge=0,le=16,strict=True)]

class CatalogReplay(Model):
    schema_version:Literal['catalog-check-replay-v1']
    release_sha:Annotated[str,Field(pattern=r'^[a-f0-9]{40}$')]|None
    request:CatalogRequest|None
    candidate:Candidate|None
    source:Source|None
    quality:Quality
    blocked:list[ReplayReason]=Field(max_length=7)

    def availability(self):
        reasons=set(self.blocked)
        for name,value in [('request',self.request),('candidate',self.candidate),('source',self.source),('release',self.release_sha)]:
            if value is None:reasons.add(name+'_unavailable')
        if self.candidate:
            if self.candidate.request!=self.request or self.request and self.candidate.environment!=self.request.environment:reasons.add('binding_rejected')
            if self.candidate.request is None or self.candidate.coverage is None or self.candidate.matching is None:reasons.add('candidate_unavailable')
        if self.source and (self.source.coverage.checked!=len(self.source.rows) or any(r.entries is not None and r.entries>1000 for r in self.source.rows)):reasons.add('source_unsupported')
        return {'scope':'catalog_check','status':'blocked' if reasons else 'available','reasons':sorted(reasons),
                'full_request_replay':False,'replayed':False,
                'limits':['Anonymous deterministic catalog check only. Original wording, model decisions and Slack delivery cannot be replayed.',
                          'Availability is a producer fixture, not proof of a successful replay or a correct answer. Use the recorded source revision and review expected facts separately.']}
