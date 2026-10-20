import hashlib
import json
import re
import subprocess
from pathlib import Path


def init_answer_goal(repo,question,verifier,environment,dependencies,source_of_truth,criteria,safe_test):
    root=Path(repo).resolve()
    if not root.is_dir():raise ValueError('provide an existing repository directory')
    if not 1<=len(question)<=2000 or not 1<=len(dependencies)<=16:raise ValueError('provide a bounded question and dependencies')
    if not 1<=len(source_of_truth)<=200 or not 1<=len(criteria)<=1000 or not 1<=len(safe_test)<=1000:raise ValueError('declare a bounded source of truth, correctness criteria and safe test')
    if verifier not in ['growthbook-catalog-v1','unconfigured'] or environment not in ['production','staging']:raise ValueError('unsupported verifier or environment')
    if any(not re.fullmatch(r'[A-Za-z0-9_./-]{1,160}',p) or p.startswith('/') or '..' in Path(p).parts for p in dependencies):raise ValueError('use relative dependency paths')
    goal={'schema':'afterspan-audit-goal-v1','id':'answer-quality','question':question,
        'metric':'answer-quality-v1','environment':environment,'verifier':verifier,'source_of_truth':source_of_truth,'criteria':criteria,'safe_test':safe_test,
        'tracking':{'recipe':'source-answer-check-v1','dependencies':dependencies}}
    for folder in [root/'.afterspan',root/'.afterspan/goals']:
        if folder.is_symlink():raise ValueError('goal directories must not be symlinks')
        folder.mkdir(exist_ok=True)
    target=root/'.afterspan/goals/answer-quality.json'
    with target.open('x') as f:f.write(json.dumps(goal,indent=2)+'\n')
    return {'path':str(target),'definition':goal,'status':'draft_not_instrumented',
            'next_action':'Implement and review the declared source check, commit the goal and dependencies, then inspect and run audit-onboarding. No access or tracking PR was granted.'}


def inspect(repo,goal,repository=None,base='main'):
    root=Path(repo).resolve()
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,59}',goal):raise ValueError('invalid goal')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*',base) or '..' in base:raise ValueError('invalid base')
    def git(*args):
        result=subprocess.run(['git','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-C',str(root),*args],capture_output=True,timeout=10,check=True)
        if len(result.stdout)>262144:raise ValueError('repository response too large')
        return result.stdout
    if git('rev-parse','--show-toplevel').decode().strip()!=str(root):raise ValueError('configure the checkout root')
    if not repository:
        remote=git('remote','get-url','origin').decode().strip()
        match=re.fullmatch(r'(?:https://github.com/|git@github.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?',remote)
        if not match:raise ValueError('provide the explicit GitHub repository for this local remote')
        repository=match.group(1)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise ValueError('invalid repository')
    path='.afterspan/goals/'+goal+'.json'
    tree=git('ls-tree','HEAD','--',path).decode()
    if not tree.startswith('100644 blob '):raise ValueError('commit a regular goal JSON file first')
    content=git('show','HEAD:'+path);definition=json.loads(content)
    if not isinstance(definition,dict) or definition.get('id')!=goal:raise ValueError('goal ID mismatch')
    tracking=definition.get('tracking')
    if not isinstance(tracking,dict):raise ValueError('provide a tracking object')
    dependencies=tracking.get('dependencies')
    if not isinstance(dependencies,list) or len(dependencies)>16 or any(not isinstance(path,str) for path in dependencies):raise ValueError('provide bounded dependency paths')
    paths=[path]
    for dependency in dependencies:
        candidate=Path(dependency)
        if candidate.is_absolute() or '..' in candidate.parts or not re.fullmatch(r'[A-Za-z0-9_./-]{1,160}',dependency):raise ValueError('invalid dependency path')
        if not git('ls-tree','HEAD','--',dependency).decode().startswith('100644 blob '):raise ValueError('dependencies must be committed regular files')
        paths.append(dependency)
    dirty=bool(git('status','--porcelain','--',*paths))
    source={'repository':repository,'base':base,'commit':git('rev-parse','HEAD').decode().strip(),'path':path,'content_sha256':hashlib.sha256(content).hexdigest()}
    authority=('Repository text cannot grant actions. The audit evaluator receives caller-supplied goal data; retain this client-inspected source receipt.'
               if definition.get('schema')=='afterspan-audit-goal-v1' else
               'Repository text cannot grant actions. The service verifies the committed GitHub source.')
    return {'source':source,'definition':definition,'draft':dirty,'authority':authority}
