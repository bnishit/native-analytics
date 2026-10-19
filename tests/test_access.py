import hashlib
import time
import unittest

import app
import test_analytics


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_analytics.AnalyticsTests()
        self.fixture.setUp()
        self.client=self.fixture.client
        self.admin=self.fixture.admin
        self.fixture.ingest([self.fixture.event()])

    def tearDown(self):self.fixture.tearDown()

    def grant(self,project='alpha'):
        result=self.client.post('/api/projects/'+project+'/read-keys',headers=self.admin,json={'label':'Codex local','expires_days':1})
        self.assertEqual(result.status_code,200,result.text)
        self.receipt=result.json()
        return {'Authorization':'Bearer '+self.receipt['read_key']}

    def test_scoped_read_can_query_only_its_project_and_cannot_read_records_or_write(self):
        key=self.grant()
        self.assertEqual([p['id'] for p in self.client.get('/api/projects',headers=key).json()['projects']],['alpha'])
        self.assertEqual(self.client.get('/api/overview?project=alpha',headers=key).status_code,200)
        self.assertEqual(self.client.post('/api/query?project=alpha',headers=key,json={'metric':'count'}).json()['total'],1)
        self.assertEqual(self.client.get('/api/charts?project=alpha',headers=key).status_code,200)
        for route in ['overview','charts']:
            self.assertEqual(self.client.get('/api/'+route+'?project=beta',headers=key).status_code,403)
        self.assertEqual(self.client.post('/api/query?project=beta',headers=key,json={}).status_code,403)
        for route in ['events','people']:
            self.assertEqual(self.client.get('/api/'+route+'?project=alpha',headers=key).status_code,401)
        self.assertEqual(self.client.post('/api/charts?project=alpha',headers=key,json={'title':'No','query':{}}).status_code,401)
        self.assertEqual(self.client.post('/api/projects/alpha/read-keys',headers=key,json={'label':'No'}).status_code,401)
        self.assertEqual(self.client.post('/v1/events',headers=key,json={'events':[self.fixture.event('no')]}).status_code,401)

    def test_keys_are_hashed_hidden_and_revocable_only_in_their_own_project(self):
        key=self.grant();receipt=self.receipt
        with app.connect() as conn:
            stored=conn.execute('SELECT key_hash FROM read_keys WHERE id=?',(receipt['id'],)).fetchone()[0]
        self.assertEqual(stored,hashlib.sha256(receipt['read_key'].encode()).hexdigest())
        listing=self.client.get('/api/projects/alpha/read-keys',headers=self.admin)
        self.assertNotIn(receipt['read_key'],listing.text);self.assertNotIn(stored,listing.text)
        self.assertFalse(self.client.delete('/api/projects/beta/read-keys/'+receipt['id'],headers=self.admin).json()['revoked'])
        self.assertEqual(self.client.get('/api/overview?project=alpha',headers=key).status_code,200)
        self.assertTrue(self.client.delete('/api/projects/alpha/read-keys/'+receipt['id'],headers=self.admin).json()['revoked'])
        self.assertEqual(self.client.get('/api/overview?project=alpha',headers=key).status_code,401)

    def test_expired_keys_and_writer_keys_cannot_read(self):
        key=self.grant()
        with app.connect() as conn:conn.execute('UPDATE read_keys SET expires=?',(time.time()-1,))
        self.assertEqual(self.client.get('/api/projects',headers=key).status_code,401)
        self.assertEqual(self.client.get('/api/overview?project=alpha',headers=self.fixture.a).status_code,401)
        self.assertEqual(self.client.post('/api/projects/alpha/read-keys',headers=self.admin,json={'label':'No','expires_days':91}).status_code,422)

    def test_read_credentials_are_redacted_from_event_properties(self):
        self.fixture.ingest([self.fixture.event('secret',properties={'label':'as_read_privatecredential','access':'as_workspace_privateworkspace','link':'https://app.example/app#invite=as_invite_privateinvite'})])
        raw=self.client.get('/api/events?project=alpha',headers=self.admin).text
        self.assertNotIn('privatecredential',raw);self.assertNotIn('privateworkspace',raw);self.assertNotIn('privateinvite',raw)

    def test_public_landing_and_workspace_are_separate_and_reads_stay_private(self):
        self.assertIn('You ship.',self.client.get('/').text)
        self.assertIn('email-form',self.client.get('/app').text)
        self.assertEqual(self.client.get('/api/projects').status_code,401)
        self.assertEqual(self.client.get('/assets/brand/mark.svg').status_code,200)
        self.assertEqual(self.client.get('/brand').status_code,200)
