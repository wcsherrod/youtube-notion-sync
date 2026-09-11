import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import sync


class Tests(unittest.TestCase):
    def test_gallery_created_once_with_expected_fields(self):
        class Fake:
            def __init__(self):
                self.payload = None
            def call(self, method, path, **kwargs):
                if path == 'views' and method == 'GET':
                    return {'results': [{'id': 'gallery'}] if self.payload else []}
                if path == 'views/gallery':
                    return {'id': 'gallery', 'name': sync.GALLERY_NAME, 'type': 'gallery'}
                if path == 'data_sources/ds':
                    return {'properties': {name: {'id': name} for name in sync.SCHEMA}}
                if path == 'views' and method == 'POST':
                    self.payload = kwargs['json']
                    return {'id': 'gallery'}
                raise AssertionError((method, path))
        api = Fake()
        self.assertEqual(sync.ensure_gallery(api, 'db', 'ds'), 'gallery')
        payload = api.payload
        self.assertEqual(payload['configuration']['cover'], {'type': 'page_cover'})
        self.assertEqual([p['property_id'] for p in payload['configuration']['properties']
                          if p['visible']], sync.GALLERY_FIELDS)
        self.assertEqual(sync.ensure_gallery(api, 'db', 'ds'), 'gallery')
        self.assertIs(api.payload, payload)

    def test_caption_failure_categories(self):
        from youtube_transcript_api._errors import (TranscriptsDisabled,
            NoTranscriptFound, RequestBlocked, VideoUnavailable)
        cases = [(TranscriptsDisabled('v'), 'No captions returned'),
                 (NoTranscriptFound('v', ['en'], []), 'No matching language'),
                 (RequestBlocked('v'), 'Blocked'),
                 (VideoUnavailable('v'), 'Video unavailable'),
                 (RuntimeError('network failure'), 'Error')]
        for error, status in cases:
            with self.subTest(status=status), patch(
                    'youtube_transcript_api.YouTubeTranscriptApi.fetch', side_effect=error):
                self.assertEqual(sync.transcript('v', {'languages': ['en']}), ('', status, ''))

    def test_database_routing_survives_rename_and_equal_names(self):
        calls = []
        class Fake:
            def call(self, method, path, **kw):
                calls.append((method, path, kw))
                return {'id': 'db' + str(len(calls)),
                        'title': kw['json'].get('title', []),
                        'data_sources': [{'id': 'ds' + str(len(calls))}]}
        api, found = Fake(), {}
        first = {'id': 'playlist1', 'snippet': {'title': 'Music'}}
        ds = sync.ensure_database(api, 'parent', first, found)
        self.assertEqual(sync.ensure_database(api, 'parent', first, found), ds)
        self.assertEqual(len(calls), 1)
        first['snippet']['title'] = 'Renamed'
        self.assertEqual(sync.ensure_database(api, 'parent', first, found), ds)
        self.assertEqual(calls[-1][0], 'PATCH')
        second = {'id': 'playlist2', 'snippet': {'title': 'Renamed'}}
        self.assertNotEqual(sync.ensure_database(api, 'parent', second, found), ds)
        self.assertEqual(calls[-1][2]['json']['parent']['page_id'], 'parent')

    def test_long_unicode_text_roundtrips(self):
        text = '🎵字幕' * 3000
        chunks = sync.rt(text)
        self.assertEqual(''.join(x['text']['content'] for x in chunks), text)
        self.assertTrue(all(len(x['text']['content']) <= 1800 for x in chunks))

    def test_publication_is_not_playlist_add_date(self):
        item = {'id': 'item1', 'snippet': {'publishedAt': '2026-09-11T00:00:00Z',
                'title': 'Fallback', 'position': 4}, 'contentDetails': {'videoId': 'v'}}
        p, _, _ = sync.properties(item, {'id': 'p', 'snippet': {'title': 'Study'}},
             {'snippet': {'title': 'Real title', 'publishedAt': '2020-01-01T00:00:00Z',
                          'channelTitle': 'Author', 'description': 'Complete description'}})
        self.assertNotEqual(p['Published'], p['Added'])
        self.assertEqual(p['Channel'], sync.rich('Author'))
        self.assertEqual(p['Position']['number'], 5)

    def test_retry_cooldown_and_completed_skip(self):
        now = datetime.now(timezone.utc)
        config = {'transcripts': 'best-effort', 'transcript_retry_days': 7}
        page = {'properties': {'Transcript status': sync.rich('Blocked'),
                              'Transcript checked': sync.date(now.isoformat())}}
        self.assertFalse(sync.should_fetch(page, now, config))
        page['properties']['Transcript checked'] = sync.date(None)
        self.assertTrue(sync.should_fetch(page, now, config))
        page['properties']['Transcript status'] = sync.rich('Full')
        self.assertFalse(sync.should_fetch(page, now, config))

    def test_youtube_pagination(self):
        class Fake:
            def call(self, method, path, params):
                return {'items': [2]} if 'pageToken' in params else {'items': [1], 'nextPageToken': 'next'}
        self.assertEqual(list(sync.yt_list(Fake(), 'playlistItems')), [1, 2])

    def test_body_preserves_user_notes(self):
        calls = []
        class Fake:
            def call(self, method, path, **kw):
                calls.append((method, path))
                if method == 'GET':
                    return {'results': [
                        {'id': 'note', 'type': 'paragraph'},
                        {'id': 'managed', 'type': 'toggle', 'toggle': {'rich_text': sync.rt(sync.MARKER)}}]}
                return {'results': [{'id': 'new'}]}
        sync.write_body(Fake(), 'page', 'long transcript ' * 5000)
        self.assertIn(('DELETE', 'blocks/managed'), calls)
        self.assertNotIn(('DELETE', 'blocks/note'), calls)
        self.assertGreater(calls.count(('PATCH', 'blocks/new/children')), 1)

    def test_unchanged_sync_is_noop_and_failed_scan_does_not_remove(self):
        playlist = {'id': 'p', 'snippet': {'title': 'Study'}}
        item = {'id': 'i', 'snippet': {'title': 'Video'}, 'contentDetails': {'videoId': 'v'}}
        props, _, _ = sync.properties(item, playlist, {})
        digest = sync.hashlib.sha256(sync.json.dumps(props, sort_keys=True).encode()).hexdigest()
        props.update({'Content hash': sync.rich(digest), 'Transcript status': sync.rich('Full')})
        old = {'id': 'page', 'properties': props}
        writes = []
        class Fake:
            def call(self, method, path, **kw):
                if method != 'GET':
                    writes.append((method, path))
                return {'items': []}
        config = {'playlist_ids': [], 'transcripts': 'off', 'transcript_retry_days': 7}
        def listing(api, resource, **kw):
            return iter([playlist] if resource == 'playlists' else [item])
        with patch.dict(sync.os.environ, {'NOTION_DATA_SOURCE_ID': 'ds'}), \
             patch.object(sync, 'notion', return_value=Fake()), \
             patch.object(sync, 'youtube', return_value=Fake()), \
             patch.object(sync, 'pages', return_value=[old]), \
             patch.object(sync, 'yt_list', side_effect=listing):
            sync.run_single(config, 'ds')
        self.assertEqual(writes, [])
        def broken(api, resource, **kw):
            if resource == 'playlists':
                return iter([playlist])
            raise RuntimeError('scan failed')
        with patch.dict(sync.os.environ, {'NOTION_DATA_SOURCE_ID': 'ds'}), \
             patch.object(sync, 'notion', return_value=Fake()), \
             patch.object(sync, 'youtube', return_value=Fake()), \
             patch.object(sync, 'pages', return_value=[old]), \
             patch.object(sync, 'yt_list', side_effect=broken):
            with self.assertRaises(RuntimeError):
                sync.run_single(config, 'ds')
        self.assertEqual(writes, [])

if __name__ == '__main__':
    unittest.main()
