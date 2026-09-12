import sys, types, unittest, io, contextlib
from unittest.mock import patch
from pathlib import Path
import requests
ConnectionError = requests.exceptions.ConnectionError
Timeout = requests.exceptions.Timeout
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync

class Response:
    def __init__(self, status=200, data=None):
        self.status_code=status; self.ok=status<400; self.headers={}; self.data=data or {}
    def json(self):return self.data

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock=0
        def sleep(t): self.clock+=max(t, 0)
        self.stack=__import__('contextlib').ExitStack()
        self.stack.enter_context(patch.object(sync.time,'sleep',sleep))
        self.stack.enter_context(patch.object(sync.time,'monotonic',lambda:self.clock))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    def tearDown(self):self.stack.close()
    def test_patch_retries_connection_then_503(self):
        api=sync.API('https://api.notion.com/v1/',{})
        with patch.object(requests,'request',side_effect=[ConnectionError(),Response(503),Response(data={'id':'ok'})]) as req:
            self.assertEqual(api.call('PATCH','pages/p',json={})['id'],'ok')
            self.assertEqual(req.call_count,3)
            self.assertGreaterEqual(self.clock,15)
    def test_exhaustion_is_bounded(self):
        with patch.object(requests,'request',side_effect=Timeout()) as req:
            with self.assertRaises(sync.TemporaryAPIError):sync.API('x',{}).call('GET','x')
            self.assertEqual(req.call_count,8)
    def test_create_lost_response_reconciles_without_second_post(self):
        api=sync.API('https://api.notion.com/v1/',{})
        payload={'parent':{'data_source_id':'ds'},'properties':{'Item ID':sync.rich('item')}}
        with patch.object(requests,'request',side_effect=ConnectionError()) as req, patch.object(sync,'pages',return_value=iter([{'id':'saved'}])):
            self.assertEqual(api.call('POST','pages',json=payload)['id'],'saved')
            self.assertEqual(req.call_count,1)
    def test_create_unknown_is_not_reposted(self):
        api=sync.API('https://api.notion.com/v1/',{})
        payload={'parent':{'data_source_id':'ds'},'properties':{'Item ID':sync.rich('item')}}
        with patch.object(requests,'request',side_effect=ConnectionError()) as req, patch.object(sync,'pages',return_value=[]):
            with self.assertRaises(sync.TemporaryAPIError):api.call('POST','pages',json=payload)
            self.assertEqual(req.call_count,1)
    def test_append_not_blindly_repeated(self):
        with patch.object(requests,'request',side_effect=ConnectionError()) as req:
            with self.assertRaises(sync.TemporaryAPIError):sync.API('https://api.notion.com/v1/',{}).call('PATCH','blocks/p/children',json={})
            self.assertEqual(req.call_count,1)
    def test_deferred_video_does_not_stop_next_video(self):
        class Fake:
            def call(self,method,path,**kw):
                return {'items':[]} if path=='videos' else {'id':'page'}
        playlist={'id':'p','snippet':{'title':'Test'}}
        items=[{'id':str(i),'snippet':{'title':'Video','position':i},'contentDetails':{'videoId':str(i)}} for i in range(2)]
        def listing(api, resource, **kw):return [playlist] if resource=='playlists' else items
        with patch.object(sync,'notion',Fake),patch.object(sync,'youtube',Fake),patch.object(sync,'pages',return_value=[]),patch.object(sync,'yt_list',side_effect=listing),patch.object(sync,'transcript',return_value=('text','Full','en')),patch.object(sync,'write_body',side_effect=[sync.TemporaryAPIError(),None]):
            result=sync.run_single({'playlist_ids':['p'],'transcripts':'best-effort','transcript_budget':50,'transcript_retry_days':7},'ds')
        self.assertEqual(result['entries_scanned'],2)
        self.assertEqual(result['deferred_writes'],1)
        self.assertEqual(result['saved_transcripts'],1)
    def test_auth_errors_not_retried(self):
        with patch.object(requests,'request',return_value=Response(401)) as req:
            with self.assertRaises(RuntimeError):sync.API('x',{}).call('GET','x')
            self.assertEqual(req.call_count,1)

if __name__ == "__main__":
    unittest.main()
