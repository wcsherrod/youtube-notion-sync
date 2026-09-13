import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from contextlib import ExitStack
import sync

class VideoCheckpointTests(unittest.TestCase):
    def test_resume_skips_committed_video_and_reuses_metadata(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / 'cache.sqlite'
            cp = sync.VideoCheckpoint(path, 'pass', 'playlist', 'ds')
            playlist = {'id':'playlist','snippet':{'title':'Test'}}
            items = [{'id':str(i),'snippet':{'title':'Video','position':i},'contentDetails':{'videoId':str(i)}} for i in range(3)]
            api = Mock()
            api.call.side_effect = lambda method,path,**kw: {'items':[]} if path=='videos' else {'id':'page'}
            stack.enter_context(patch.object(sync,'notion',return_value=api))
            stack.enter_context(patch.object(sync,'pages',return_value=[]))
            listing = stack.enter_context(patch.object(sync,'yt_list',return_value=items))
            stack.enter_context(patch.object(sync.progress,'log'))
            config={'playlist_ids':['playlist'],'transcripts':'off','transcript_budget':0,'_video_checkpoint':cp}
            with patch.object(sync,'write_body',side_effect=[None,KeyboardInterrupt()]):
                with self.assertRaises(KeyboardInterrupt):sync.run_single(config,'ds',playlist,api)
            self.assertTrue(cp.get('done:0'))
            self.assertIsNone(cp.get('done:1'))
            cp.close()
            cp=sync.VideoCheckpoint(path,'pass','playlist','ds'); config['_video_checkpoint']=cp
            api.reset_mock()
            with patch.object(sync,'write_body') as body:
                result=sync.run_single(config,'ds',playlist,api)
                self.assertEqual(body.call_count,2)
            self.assertEqual(result['checkpoint_skipped'],1)
            self.assertEqual(listing.call_count,1)
            self.assertFalse(any(c.args[1]=='videos' for c in api.call.call_args_list))
            cp.close()
            cp=sync.VideoCheckpoint(path,'new-pass','playlist','ds')
            self.assertIsNone(cp.get('source'))
            self.assertIsNone(cp.get('done:0'))
            cp.close()

    def test_incomplete_listing_is_not_published(self):
        with tempfile.TemporaryDirectory() as folder:
            cp=sync.VideoCheckpoint(Path(folder)/'cache.sqlite','pass','p','ds')
            def broken(*args,**kw):
                yield {'id':'one'}
                raise sync.PlaylistUnavailable()
            with patch.object(sync,'notion'),patch.object(sync,'pages',return_value=[]),patch.object(sync,'yt_list',side_effect=broken):
                with self.assertRaises(sync.PlaylistUnavailable):
                    sync.run_single({'transcript_budget':0,'_video_checkpoint':cp},'ds',{'id':'p'},Mock())
            self.assertIsNone(cp.get('source'))
            cp.close()

    def test_oauth_transport_overrides_long_timeout(self):
        with patch('google.auth.transport.requests.Request') as factory:
            sync.oauth_request()('url',timeout=120)
            self.assertEqual(factory.return_value.call_args.kwargs['timeout'],(10,30))

    def test_playlist_unavailable_distinct_from_quota(self):
        for code,status,expected in [('playlistNotFound',404,sync.PlaylistUnavailable),('quotaExceeded',403,sync.SyncError)]:
            response=Mock(ok=False,status_code=status)
            response.json.return_value={'error':{'errors':[{'reason':code}]}}
            with patch.object(sync.requests,'request',return_value=response),patch.object(sync.time,'sleep'):
                with self.assertRaises(expected) as ctx:sync.API('url',{}).call('GET','playlistItems')
                if code=='quotaExceeded':self.assertNotIsInstance(ctx.exception,sync.PlaylistUnavailable)
