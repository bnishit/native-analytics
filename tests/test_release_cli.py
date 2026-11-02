import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

PATH=Path(__file__).resolve().parents[1]/'cli/native_analytics.py'
spec=importlib.util.spec_from_file_location('release_test_cli',PATH)
cli=importlib.util.module_from_spec(spec);sys.modules[spec.name]=cli;spec.loader.exec_module(cli)
ID='7dba2cd3-6ca1-4a54-875c-a66f8cc6aebe'

class ReleaseCliTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.env={'NATIVE_ANALYTICS_URL':'https://analytics.example.test','AFTERSPAN_WORKSPACE_KEY':'as_workspace_fixture'}
 def private(self,name,value):
  path=Path(self.temp.name)/name;path.write_text(json.dumps(value));path.chmod(0o600);return str(path)
 def call(self,args,env=None):
  output=io.StringIO()
  with patch.dict(os.environ,env or self.env,clear=True),patch.object(cli.urllib.request,'build_opener') as opener,contextlib.redirect_stdout(output):
   opener.return_value.open.return_value=io.BytesIO(b'{"status":"pending"}')
   self.assertEqual(cli.main(args),0)
   request=opener.return_value.open.call_args.args[0]
  self.assertEqual(json.loads(output.getvalue()),{'status':'pending'})
  self.assertEqual(parse_qs(urlsplit(request.full_url).query)['project'],['app'])
  return request
 def test_prepare_poll_sources_and_final_read_keep_stable_ids(self):
  body={'commit':'a'*40,'pr_number':12}
  file=self.private('proposal.json',body)
  for command,path,method,extra in [
   ('release-sources','/api/outcomes/release-sources','GET',[]),
   ('release-prepare','/api/outcomes/release-proposals/'+ID,'PUT',['--id',ID,'--file',file]),
   ('release-proposal','/api/outcomes/release-proposals/'+ID,'GET',['--id',ID]),
   ('release-evidence','/api/outcomes/releases/'+ID,'GET',['--id',ID])]:
   with self.subTest(command=command):
    request=self.call([command,'--project','app',*extra])
    self.assertEqual(urlsplit(request.full_url).path,path);self.assertEqual(request.get_method(),method)
    self.assertEqual(json.loads(request.data),body) if method=='PUT' else self.assertIsNone(request.data)
 def test_adoption_requires_account_receipt_and_never_sends_workspace_key(self):
  body={'proposal_id':ID,'proposal_sha256':'b'*64};file=self.private('adopt.json',body)
  args=['release-adopt','--project','app','--id',ID,'--file',file]
  with patch.dict(os.environ,self.env,clear=True),patch.object(cli.urllib.request,'build_opener') as opener:
   with self.assertRaisesRegex(ValueError,'account session'):cli.main(args)
   opener.assert_not_called()
  session=self.private('session.json',{'origin':self.env['NATIVE_ANALYTICS_URL'],'cookie_name':'as_session','session':'as_session_fixture'})
  request=self.call(args,{**self.env,'AFTERSPAN_SESSION_FILE':session})
  self.assertEqual(request.get_method(),'POST');self.assertTrue(urlsplit(request.full_url).path.endswith('/'+ID+'/adopt'))
  self.assertEqual(request.get_header('Cookie'),'as_session=as_session_fixture')
  self.assertIsNone(request.get_header('Authorization'));self.assertEqual(json.loads(request.data),body)
