import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import Mock, patch
import sync

class LongRunSafetyTests(unittest.TestCase):
    def test_caption_requests_have_timeout(self):
        with sync.CaptionSession() as session, patch.object(sync.requests.Session, 'request') as request:
            session.get('https://example.com', timeout=None)
            self.assertEqual(request.call_args.kwargs['timeout'], (10, 30))

    def test_failed_caption_save_clears_old_hash_and_reuses_caption(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            cp = sync.VideoCheckpoint(Path(folder)/'cache.sqlite', 'pass', 'p', 'ds')
            stack.callback(cp.close)
            playlist = {'id':'p','snippet':{'title':'Test'}}
            item = {'id':'i','snippet':{'title':'Video','position':0},'contentDetails':{'videoId':'v'}}
            props, _, _ = sync.properties(item, playlist, {})
            digest = hashlib.sha256(json.dumps(props, sort_keys=True).encode()).hexdigest()
            props.update({'Content hash':sync.rich(digest), 'Transcript status':sync.rich('Pending')})
            row = {'id':'row','properties':props}
            def call(method, path, **kw):
                if path == 'videos': return {'items':[]}
                if path == 'pages/row': row['properties'].update(copy.deepcopy(kw['json']['properties']))
                return {'id':'row'}
            api = Mock(); api.call.side_effect = call
            stack.enter_context(patch.object(sync, 'notion', return_value=api))
            stack.enter_context(patch.object(sync, 'pages', side_effect=lambda *a: [copy.deepcopy(row)]))
            stack.enter_context(patch.object(sync, 'yt_list', return_value=[item]))
            stack.enter_context(patch.object(sync.progress, 'log'))
            stack.enter_context(patch.object(sync.time, 'sleep'))
            config = {'transcripts':'best-effort','transcript_budget':50,'transcript_retry_days':7,'_video_checkpoint':cp}
            with patch.object(sync, 'transcript', return_value=('captions','Full','en')) as fetch, patch.object(sync, 'write_body', side_effect=sync.TemporaryAPIError()):
                result = sync.run_single(config, 'ds', playlist, api)
                self.assertEqual(result['deferred_writes'], 1)
                fetch.assert_called_once()
            self.assertEqual(sync.plain(row, 'Content hash'), '')
            self.assertIsNone(cp.get('done:i'))
            with patch.object(sync, 'transcript') as fetch, patch.object(sync, 'write_body') as body:
                sync.run_single(config, 'ds', playlist, api)
                fetch.assert_not_called()
                self.assertIn('captions', body.call_args.args[2])
            self.assertTrue(cp.get('done:i'))
            self.assertEqual(sync.plain(row, 'Transcript status'), 'Full')
            self.assertTrue(sync.plain(row, 'Content hash'))

    def test_gallery_failure_defers_playlist_and_continues(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            config = {'notion_parent_page_id':'parent','playlist_ids':[],'checkpoint_file':str(Path(folder)/'cp.json'),'transcript_budget':0}
            playlists = [{'id':x,'snippet':{'title':x}} for x in ('a','b')]
            stack.enter_context(patch.dict(sync.os.environ, {'GITHUB_ACTIONS':'','NOTION_PARENT_PAGE_ID':'','YOUTUBE_TOKEN_JSON':'{}'}))
            stack.enter_context(patch.object(sync, 'notion', return_value=Mock(base='url',headers={})))
            stack.enter_context(patch.object(sync, 'youtube'))
            stack.enter_context(patch.object(sync, 'yt_list', return_value=playlists))
            stack.enter_context(patch.object(sync, 'playlist_databases', return_value={x:{'id':x} for x in ('a','b')}))
            stack.enter_context(patch.object(sync, 'ensure_database', return_value='ds'))
            stack.enter_context(patch.object(sync, 'ensure_gallery', side_effect=[sync.TemporaryAPIError(),None]))
            stack.enter_context(patch.object(sync.progress, 'log'))
            with patch.object(sync, 'run_single', return_value={}) as run:
                sync.run(config)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.kwargs['playlist']['id'], 'b')
            self.assertEqual(json.loads(Path(config['checkpoint_file']).read_text())['completed_playlist_ids'], ['b'])

    def test_completed_playlists_revisited_while_another_stays_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'cp.json'
            with patch.object(sync.time, 'time', return_value=100000):
                state = sync.load_checkpoint(path, 'parent', ['a','b'])
                sync.mark_playlist_complete(path, state, 'a')
            with patch.object(sync.time, 'time', return_value=100001):
                resumed = sync.load_checkpoint(path, 'parent', ['a','b'])
                self.assertEqual(resumed['completed_playlist_ids'], ['a'])
            with patch.object(sync.time, 'time', return_value=121601):
                resumed = sync.load_checkpoint(path, 'parent', ['a','b'])
                self.assertEqual(resumed['completed_playlist_ids'], [])
                self.assertEqual(resumed['playlist_generations']['a'], 1)
                self.assertEqual(resumed['playlist_ids'], ['b', 'a'])
                self.assertEqual(resumed['pass_id'], state['pass_id'])
