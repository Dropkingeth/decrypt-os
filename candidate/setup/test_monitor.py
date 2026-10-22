import fcntl
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import monitor


class WorkFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / 'setup'
        self.guided = monitor.load_guided()
    def tearDown(self):
        self.temp.cleanup()
    def state(self, status):
        self.work.mkdir(exist_ok=True)
        (self.work / 'STATE.json').write_text(json.dumps({'schema': self.guided.SCHEMA, 'status': status}))


class StatusTests(WorkFixture):
    def test_missing_state_does_not_create_work(self):
        self.assertEqual(self.guided.current_state(self.work)['status'], 'not_started')
        self.assertFalse(self.work.exists())
    def test_stale_operation_is_interrupted(self):
        self.state('installing')
        got = self.guided.current_state(self.work)
        self.assertEqual(got['status'], 'interrupted')
        self.assertEqual(got['lastRecordedStatus'], 'installing')
    def test_held_lock_keeps_operation_active(self):
        self.state('preparing')
        with (self.work / '.setup.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = self.guided.current_state(self.work)
            self.assertEqual(got['status'], 'preparing')
            self.assertTrue(got['operationActive'])
    def test_projection_drops_private_fields(self):
        result = monitor.projection({'status': 'installed', 'machine': 'private', 'message': 'secret', 'attempt': '/private/location', 'verification': {'secret':'not-for-browser'}})
        self.assertEqual(set(result), {'mode','status','source','checkedAt'})
        self.assertEqual(result['status'], 'installed')
    def test_unknown_state_is_unavailable(self):
        self.assertEqual(monitor.projection({'status':'arbitrary'})['status'], 'unavailable')
    def test_changed_reader_rejected(self):
        p=self.root/'reader.py';p.write_text('raise Exception("must not execute")')
        with self.assertRaises(ValueError):monitor.load_guided(p)
    def test_reader_link_rejected(self):
        p=self.root/'reader.py';p.symlink_to(monitor.GUIDED)
        with self.assertRaises(ValueError):monitor.load_guided(p)


class HttpTests(WorkFixture):
    def setUp(self):
        super().setUp()
        self.server=monitor.Monitor(self.work,guided=self.guided)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        super().tearDown()
    def get(self,path='/',method='GET',headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        conn.request(method,path,headers=headers or {})
        response=conn.getresponse();data=response.read();result=(response.status,dict(response.getheaders()),data);conn.close();return result
    def test_http_missing_state_does_not_write(self):
        code,_,data=self.get('/api/status')
        self.assertEqual(code,200);self.assertEqual(json.loads(data)['status'],'not_started');self.assertFalse(self.work.exists())
    def test_malformed_record_is_unavailable(self):
        self.work.mkdir();(self.work/'STATE.json').write_text('broken')
        code,_,data=self.get('/api/status')
        self.assertEqual(code,200);self.assertEqual(json.loads(data)['status'],'unavailable')
    def test_live_state_cannot_be_selected(self):
        self.assertEqual(self.get('/api/status?state=installed')[0],400)
    def test_non_object_record_is_unavailable(self):
        self.work.mkdir();(self.work/'STATE.json').write_text('[]')
        self.assertEqual(json.loads(self.get('/api/status')[2])['status'],'unavailable')
    def test_invalid_status_type_is_unavailable(self):
        self.work.mkdir();(self.work/'STATE.json').write_text(json.dumps({'schema':self.guided.SCHEMA,'status':[]}))
        self.assertEqual(json.loads(self.get('/api/status')[2])['status'],'unavailable')
    def test_preview_is_labeled_and_does_not_read_work(self):
        self.server.preview=True
        data=json.loads(self.get('/api/status?state=installed')[2])
        self.assertEqual(data['mode'],'preview');self.assertEqual(data['status'],'installed');self.assertFalse(self.work.exists())
    def test_unknown_preview_rejected(self):
        self.server.preview=True;self.assertEqual(self.get('/api/status?state=green')[0],400)
    def test_mutation_route_absent(self):
        self.assertEqual(self.get('/api/install',method='POST')[0],405)
    def test_only_known_files_served(self):
        for path in ('/../monitor.py','/%2e%2e/monitor.py','/monitor.py','/STATE.json'):
            self.assertEqual(self.get(path)[0],404)
    def test_foreign_origin_or_host_rejected(self):
        self.assertEqual(self.get('/api/status',headers={'Origin':'https://example.com'})[0],403)
        self.assertEqual(self.get('/api/status',headers={'Host':'example.com'})[0],403)
    def test_page_headers_and_head(self):
        code,headers,data=self.get('/')
        self.assertEqual(code,200);self.assertIn(b'Decrypt OS',data);self.assertEqual(headers['Cache-Control'],'no-store');self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(self.get('/',method='HEAD')[2],b'')

if __name__=='__main__':unittest.main()
