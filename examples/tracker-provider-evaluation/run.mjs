import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { digest } from './transport.mjs';

export function parseArguments(args) {
  const result = {};
  while (args.length) {
    const flag = args.shift();
    if (['--live','--offline'].includes(flag)) { if (result.mode) throw Error('Choose one evaluation mode'); result.mode=flag.slice(2); }
    else if (['--repo','--source','--runtime','--variant','--case','--output','--project','--service','--environment','--negative-control'].includes(flag)) { const name=flag.slice(2);if(result[name]||!args.length||args[0].startsWith('--'))throw Error('Missing or duplicate option');result[name]=args.shift(); }
    else throw Error('Unsupported option');
  }
  if (!result.mode||!['baseline','candidate'].includes(result.variant)||!['bounded-five-enabled','total-enabled','full-list-capped'].includes(result.case)||!result.repo||!result.output||!/^[a-f0-9]{40}$/.test(result.source||'')) throw Error('Exact source, variant, case, output and mode required');
  if (result.mode==='live'&&(!/^[a-f0-9]{40}$/.test(result.runtime||'')||['project','service','environment'].some(key=>!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(result[key]||'')))) throw Error('Exact deployed runtime and Railway target required');
  if(result['negative-control']&&(result.mode!=='offline'||result['negative-control']!=='caught_request_post'))throw Error('Negative controls require offline mode');
  return result;
}
export function run(options) {
  const repo=fs.realpathSync(options.repo),directory=path.resolve(options.output),here=path.dirname(fileURLToPath(import.meta.url));
  function command(binary,args,extra={}){const result=spawnSync(binary,args,{cwd:repo,encoding:'utf8',maxBuffer:64*1024*1024,timeout:120000,...extra});if(result.status!==0)throw Error('Evaluation preparation failed');return result.stdout;}
  const git=(...args)=>command('git',['-C',repo,...args]).trim();
  if(git('rev-parse','--verify',options.source+'^{commit}')!==options.source)throw Error('Exact source commit missing');
  const sourceLock=command('git',['-C',repo,'show',options.source+':package-lock.json']);
  if(sourceLock!==fs.readFileSync(path.join(repo,'package-lock.json'),'utf8')){
    const actual=JSON.parse(fs.readFileSync(path.join(repo,'package-lock.json'))),wanted=JSON.parse(sourceLock);
    for(const lock of [actual,wanted]){delete lock.packages['node_modules/@afterspan/sdk'];delete lock.packages[''].dependencies?.['@afterspan/sdk'];}
    if(JSON.stringify(actual)!==JSON.stringify(wanted))throw Error('Dependency differences exceed the recorded SDK migration');
  }
  const vitest=path.join(repo,'node_modules/vitest/vitest.mjs');if(!fs.existsSync(vitest))throw Error('Install the checkout dependencies first');
  const installed=JSON.parse(fs.readFileSync(path.join(repo,'node_modules/.package-lock.json'))),lock=JSON.parse(fs.readFileSync(path.join(repo,'package-lock.json')));
  for(const name of ['vitest','zod','@afterspan/sdk']){const key='node_modules/'+name;if(!installed.packages[key]||installed.packages[key].version!==lock.packages[key]?.version||installed.packages[key].integrity!==lock.packages[key]?.integrity)throw Error('Installed dependency binding changed');}
  fs.mkdirSync(directory,{mode:0o700});
  const sourceDirectory=path.join(directory,'source');fs.mkdirSync(sourceDirectory,{mode:0o700});
  const archive=spawnSync('git',['-C',repo,'archive',options.source],{maxBuffer:64*1024*1024});if(archive.status!==0)throw Error('Source archive unavailable');command('tar',['-x','-C',sourceDirectory],{input:archive.stdout});
  fs.symlinkSync(path.join(repo,'node_modules'),path.join(sourceDirectory,'node_modules'),'dir');
  for(const [from,to] of [['agent.fixture.ts','provider-evaluation.fixture.ts'],['transport.mjs','evaluation-transport.mjs']])fs.copyFileSync(path.join(here,from),path.join(sourceDirectory,to));
  const plan={mode:options.mode,source_sha:options.source,runtime_sha:options.runtime,variant:options.variant,case:options.case,project:options.project,service:options.service,environment:options.environment,directory,negative_control:options['negative-control']};
  const planPath=path.join(directory,'plan.json');fs.writeFileSync(planPath,JSON.stringify(plan),{mode:0o600});
  const resultPath=path.join(directory,'case.json'),reportPath=path.join(directory,'vitest.json'),config=path.join(sourceDirectory,'evaluation.config.mjs');
  fs.writeFileSync(config,`export default {root:${JSON.stringify(sourceDirectory)},cacheDir:${JSON.stringify(path.join(directory,'cache'))},resolve:{alias:{'@':${JSON.stringify(path.join(sourceDirectory,'src'))}}},test:{environment:'node',include:['provider-evaluation.fixture.ts'],fileParallelism:false,maxWorkers:1}}`,{mode:0o600});
  const env=Object.fromEntries(['PATH','HOME','USER','TMPDIR'].filter(key=>process.env[key]!==undefined).map(key=>[key,process.env[key]]));
  const execution=spawnSync(process.execPath,[vitest,'run','--config',config,'--configLoader','runner','--reporter=json','--outputFile',reportPath],{cwd:sourceDirectory,env:{...env,PROVIDER_EVALUATION_PLAN:planPath,PROVIDER_EVALUATION_RESULT:resultPath},encoding:'utf8',timeout:120000,maxBuffer:2*1024*1024});
  fs.writeFileSync(path.join(directory,'execution.log'),(execution.stdout||'')+(execution.stderr||''),{mode:0o600});
  if(execution.status!==0)throw Error('Selected execution failed; retain this attempt and inspect the private log');
  const report=JSON.parse(fs.readFileSync(reportPath));if(!report.success||report.numTotalTests!==1||report.numPassedTests!==1||report.numPendingTests||report.numFailedTests)throw Error('Selected nonempty execution not proved');
  const result=JSON.parse(fs.readFileSync(resultPath));if(result.sha!==options.source||result.variant!==options.variant||result.case!==options.case||result.results.length!==1)throw Error('Execution identity mismatch');
  const manifest={kind:'tracker-read-only-provider-evaluation',mode:options.mode,source_sha:options.source,runtime_sha:options.runtime,variant:options.variant,case:options.case,node:process.version,vitest:installed.packages['node_modules/vitest'].version,package_lock_sha256:digest(fs.readFileSync(path.join(repo,'package-lock.json'))),source_lock_sha256:digest(sourceLock),runner_sha256:digest(fs.readFileSync(fileURLToPath(import.meta.url))),harness_sha256:digest(fs.readFileSync(path.join(here,'agent.fixture.ts'))),transport_sha256:digest(fs.readFileSync(path.join(here,'transport.mjs'))),limits:['Typed read-agent entry; upstream parser and Slack delivery not exercised.','Synthetic fixed catalog; no live GrowthBook reads or writes.','Application elapsed includes SSH transport; not provider latency.','At most three provider calls, no automatic retry or resume.','No prompt, model text or raw provider response persisted.']};
  const artifact=path.join(directory,'result.json');fs.writeFileSync(artifact,JSON.stringify({manifest,result:result.results[0]},null,2),{mode:0o600});return {artifact,mode:options.mode,case:options.case,model_calls:result.results[0].model_calls,error_code:result.results[0].error_code||null};
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  try{console.log(JSON.stringify(run(parseArguments(process.argv.slice(2)))));}catch{console.error('Evaluation failed; inspect the retained private attempt. No provider action is automatically retried.');process.exitCode=1;}
}
