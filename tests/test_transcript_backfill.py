import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sync
import transcript_backfill as backfill


def page(pid, video):
    return {'id': pid, 'properties': {'Video ID': sync.rich(video),
            'Transcript status': sync.rich('Pending')}}


class BackfillTests(unittest.TestCase):
    def execute(self, directory, rows, fetch, fail_body=False):
        class API:
            def call(self, method, path, **kwargs):
                if method == 'GET':
                    return next(p for p in rows if p['id'] == path.split('/')[-1])
                return {}
        with patch.object(sync, 'notion', return_value=API()), patch.object(
                sync, 'playlist_databases', return_value={'p': {'data_sources': [{'id': 'ds'}]}}), patch.object(
                sync, 'pages', return_value=rows), patch.object(sync, 'transcript', side_effect=fetch) as fetched, patch.object(
                sync, 'write_body', side_effect=sync.TemporaryAPIError('test failure') if fail_body else None) as body, patch.object(
                backfill.time, 'sleep'), patch.dict(sync.os.environ, {}, clear=True):
            result = backfill.run({'notion_parent_page_id': 'parent', 'transcript_retry_days': 7},
                                  2, 1, str(Path(directory) / 'cache.sqlite'))
            return result, fetched.call_count, body.call_count

    def test_duplicate_videos_fetch_once_and_deliver_every_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            result, fetches, writes = self.execute(directory, [page('1', 'v'), page('2', 'v')],
                                                   lambda *args: ('caption', 'Full', 'en'))
            self.assertEqual((fetches, writes, result['saved_pages']), (1, 2, 2))

    def test_failed_writes_reuse_durable_caption_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [page('1', 'v')]
            result, _, _ = self.execute(directory, rows, lambda *args: ('caption', 'Full', 'en'), True)
            self.assertEqual(result['deferred_pages'], 1)
            result, fetches, writes = self.execute(directory, rows, lambda *args: self.fail('re-fetched'))
            self.assertEqual((fetches, writes, result['saved_pages']), (0, 1, 1))

    def test_block_stops_other_videos_and_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [page('1', 'a'), page('2', 'b')]
            _, fetches, _ = self.execute(directory, rows, lambda *args: ('', 'Blocked', ''))
            self.assertEqual(fetches, 1)
            _, fetches, _ = self.execute(directory, rows, lambda *args: self.fail('ignored cooldown'))
            self.assertEqual(fetches, 0)

    def test_budget_limits_unique_fetches(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [page(str(i), str(i)) for i in range(4)]
            _, fetches, _ = self.execute(directory, rows, lambda *args: ('caption', 'Full', 'en'))
            self.assertEqual(fetches, 2)
