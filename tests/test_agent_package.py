"""Package resolution fails closed before a client can load wrong paths or guides."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('package_check',ROOT/'scripts/check_agent_package.py')
package=importlib.util.module_from_spec(spec);spec.loader.exec_module(package)


class AgentPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'package';self.root.mkdir()
        for name in ['.codex-plugin','.claude-plugin','.agents/plugins','skills','static/brand']:
            shutil.copytree(ROOT/name,self.root/name)
        for name in ['.mcp.json','remote_mcp.py']:shutil.copyfile(ROOT/name,self.root/name)
    def tearDown(self):self.temp.cleanup()
    def change(self,path,edit):
        p=self.root/path;b=json.loads(p.read_text());edit(b);p.write_text(json.dumps(b))
    def test_local_and_remote_guides_share_the_same_package(self):
        result=package.check(self.root)
        self.assertEqual(result['plugin'],'afterspan@afterspan-local');self.assertIn('improve-tracker',result['workflows'])
    def test_marketplace_cannot_escape_or_point_to_a_sibling(self):
        self.change('.agents/plugins/marketplace.json',lambda b:b['plugins'][0]['source'].update(path='./..'))
        with self.assertRaises(ValueError):package.check(self.root)
    def test_top_level_hook_cannot_mask_the_missing_supported_extension(self):
        def misplaced(b):
            extension=b.setdefault('extensions',{}).setdefault('com.openai',{})
            b['onboardingSkill']=extension.pop('onboardingSkill','./skills/connect-and-verify/SKILL.md')
        self.change('.codex-plugin/plugin.json',misplaced)
        with self.assertRaises(ValueError):package.check(self.root)
    def test_missing_setup_or_remote_guide_is_a_failure(self):
        (self.root/'skills/connect-and-verify/SKILL.md').unlink()
        with self.assertRaises(ValueError):package.check(self.root)
    def test_remote_workflow_drift_is_not_a_valid_install(self):
        p=self.root/'remote_mcp.py';p.write_text(p.read_text().replace("'improve-tracker'", "'unpackaged-flow'"))
        with self.assertRaises(ValueError):package.check(self.root)
    def test_listed_guide_must_also_be_accepted_by_the_tool(self):
        p=self.root/'remote_mcp.py';text=p.read_text();start=text.index('def get_workflow(')
        p.write_text(text[:start]+text[start:].replace("'improve-tracker'","'unknown-guide'",1))
        with self.assertRaises(ValueError):package.check(self.root)
    def test_skill_symlink_cannot_load_instructions_outside_the_package(self):
        external=Path(self.temp.name)/'external-skill.md';external.write_text('external input')
        path=self.root/'skills/improve-tracker/SKILL.md';path.unlink();path.symlink_to(external)
        with self.assertRaises(ValueError):package.check(self.root)
    def test_credentials_and_remote_origins_never_enter_the_package(self):
        self.change('.codex-plugin/mcp.json',lambda b:b['mcpServers'].update(afterspan={'type':'http','url':'${AFTERSPAN_URL}/mcp'}))
        with self.assertRaises(ValueError):package.check(self.root)
        for value in ['https://token@host.example','https://host.example/?key=secret','http://remote.example','https://host.example/mcp','https://host.example:invalid','https://host.example?','https://host.example#']:
            with self.assertRaises(ValueError):package.origin(value)
        self.assertEqual(package.origin('https://own.example/'),'https://own.example')
        self.assertEqual(package.origin('http://127.0.0.1:8786'),'http://127.0.0.1:8786')


class AgentSetupWebTests(unittest.TestCase):
    def test_same_origin_setup_runs_under_the_service_csp(self):
        import subprocess
        import app
        import test_analytics
        fixture=test_analytics.AnalyticsTests();fixture.setUp()
        try:
            response=fixture.client.get('/docs')
            self.assertEqual(response.status_code,200)
            policy=response.headers['content-security-policy'].split('script-src ')[1].split(';')[0]
            self.assertEqual(policy,"'self'")
            self.assertTrue('<script src="/assets/agent-setup.js" defer></script>' in response.text,'Setup script must be an external same-origin asset')
            self.assertNotIn('<script>',response.text)
            asset=fixture.client.get('/assets/agent-setup.js');self.assertEqual(asset.status_code,200)
            harness="const vm=require('node:vm');const field={textContent:''},task={value:process.argv[2]};vm.runInNewContext(JSON.parse(process.argv[1]),{document:{getElementById:id=>id==='tracker-setup'?field:task},location:{origin:'http://127.0.0.1:8786'}});process.stdout.write(field.textContent);"
            run=subprocess.run(['node','-e',harness,json.dumps(asset.text)],capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(run.stdout,'codex mcp add afterspan --url http://127.0.0.1:8786/mcp\ncodex mcp login afterspan --scopes analytics:read')
            for task,scope in [('audit:read','audit:read'),('charts:write','analytics:read')]:
                run=subprocess.run(['node','-e',harness,json.dumps(asset.text),task],capture_output=True,text=True,timeout=10)
                self.assertEqual(run.returncode,0,run.stderr)
                self.assertTrue(run.stdout.endswith('--scopes '+scope))
            self.assertIn('Check usage and metrics',response.text)
            self.assertIn('Start a new chat',response.text)
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
