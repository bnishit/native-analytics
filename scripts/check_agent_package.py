"""Check the local agent package and optional Codex discovery without installing it."""
import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


def local(root,value):
    if not isinstance(value,str) or not value.startswith('./'):raise ValueError('package paths must start with ./')
    path=(root/value).resolve()
    if not path.is_relative_to(root.resolve()) or not path.exists():raise ValueError('package reference is absent or outside the package')
    return path


def check(root):
    root=root.resolve()
    load=lambda name:json.loads((root/name).read_text())
    codex=load('.codex-plugin/plugin.json');claude=load('.claude-plugin/plugin.json')
    if codex['name']!='afterspan' or codex['name']!=claude['name'] or codex['version']!=claude['version']:raise ValueError('plugin identities or versions disagree')
    if not re.fullmatch(r'\d+\.\d+\.\d+',codex['version']):raise ValueError('plugin requires a release version')
    if not codex.get('author',{}).get('name') or not codex.get('description'):raise ValueError('plugin publisher and purpose are required')
    if 'onboardingSkill' in codex:raise ValueError('setup hook belongs in extensions.com.openai, not the manifest root')
    skills=local(root,codex['skills'])
    onboarding=local(root,codex.get('extensions',{}).get('com.openai',{}).get('onboardingSkill'))
    if onboarding!=skills/'connect-and-verify'/'SKILL.md':raise ValueError('setup must load the connection workflow')
    names=[]
    for path in sorted(skills.glob('*/SKILL.md')):
        local(root,'./'+str(path.relative_to(root)))
        text=path.read_text();match=re.match(r'---\nname: ([a-z][a-z0-9-]*)\ndescription: (.+)\n---\n',text)
        if not match or match[1]!=path.parent.name:raise ValueError('skill metadata disagrees with its package path')
        names.append(match[1])
    tree=ast.parse((root/'remote_mcp.py').read_text())
    workflows=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='WORKFLOWS' for t in n.targets))
    if set(names)!=set(workflows) or 'improve-tracker' not in names:raise ValueError('packaged guides and remote workflow discovery disagree')
    guide=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='get_workflow')
    accepted=ast.literal_eval(guide.args.args[0].annotation.slice)
    if set(accepted)!=set(names):raise ValueError('remote guide invocation does not accept every packaged workflow')
    mcp=json.loads(local(root,codex['mcpServers']).read_text())
    if mcp!={'mcpServers':{}}:raise ValueError('Codex setup must register the chosen deployment explicitly; bundled URL interpolation is unsupported')
    if load('.mcp.json')!={'mcpServers':{'afterspan':{'type':'http','url':'${AFTERSPAN_URL}/mcp'}}}:raise ValueError('Claude compatibility must use the caller deployment without credentials')
    interface=codex['interface']
    for field in ['composerIcon','logo']:
        icon=local(root,interface[field]);box=ET.parse(icon).getroot().attrib.get('viewBox','').split()
        if len(box)!=4 or float(box[2])!=float(box[3]) or float(box[2])<48:raise ValueError('plugin icon must have a square viewBox of at least 48')
    market=load('.agents/plugins/marketplace.json')
    if len(market['plugins'])!=1:raise ValueError('marketplace must select this package only')
    entry=market['plugins'][0]
    if entry['name']!=codex['name'] or entry['source']['source']!='local' or local(root,entry['source']['path'])!=root:raise ValueError('marketplace points to a different plugin')
    if entry['policy']!={'installation':'AVAILABLE','authentication':'ON_INSTALL'}:raise ValueError('installation and authentication must remain explicit')
    return {'package_valid':True,'plugin':codex['name']+'@'+market['name'],'version':codex['version'],'workflows':names}


def discover(root,expected):
    args=['codex','-c','marketplaces.afterspan-local.source_type="local"','-c',
          'marketplaces.afterspan-local.source='+json.dumps(str(root.resolve())),
          'plugin','list','--marketplace','afterspan-local','--available','--json']
    run=subprocess.run(args,capture_output=True,text=True,timeout=45)
    if run.returncode:raise ValueError('Codex package discovery failed; no install was attempted')
    rows=json.loads(run.stdout).get('available',[])
    selected=[r for r in rows if r.get('pluginId')==expected['plugin'] and r.get('version')==expected['version']]
    if len(selected)!=1:raise ValueError('Codex did not discover the exact package version')
    return {'available':True,'installed':selected[0]['installed'],'enabled':selected[0]['enabled']}


def origin(value):
    url=urlsplit(value)
    if '?' in value or '#' in value or (url.scheme!='https' and not(url.scheme=='http' and url.hostname in {'127.0.0.1','localhost','::1'})) or not url.hostname or url.username or url.password or url.path not in {'','/'} or url.query or url.fragment:
        raise ValueError('use your credential-free HTTPS origin, or loopback HTTP')
    url.port  # Reject malformed ports before emitting a client URL.
    return value.rstrip('/')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--codex',action='store_true',help='Check command-local marketplace discovery; does not install or sign in')
    parser.add_argument('--origin',help='Validate the caller-supplied deployment origin; no network request')
    args=parser.parse_args()
    try:
        result=check(args.root)
        if args.codex:result['codex_discovery']=discover(args.root,result)
        if args.origin:result['connection']={'mcp_url':origin(args.origin)+'/mcp','authentication_required':True}
        result['limits']=['Discovery is not installation, OAuth consent, a completed agent task or public publication.','Codex loads packaged skills; connect-and-verify registers the chosen deployment through codex mcp add before OAuth.']
        print(json.dumps(result,indent=2))
    except (ValueError,KeyError,StopIteration,OSError,ET.ParseError,subprocess.TimeoutExpired) as exc:
        parser.exit(1,'Agent package check failed: '+str(exc)+'\n')


if __name__=='__main__':main()
