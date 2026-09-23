import tempfile
import sqlite3
import json
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import sync
import transcript_backfill as backfill


def page(pid, video):
    return {'id': pid, 'properties': {'Video ID': sync.rich(video),
            'Transcript status': sync.rich('Pending')}}


class BackfillTests(unittest.TestCase):
    def execute(self, directory, rows, fetch, fail_body=False, wait_on_block=False, discovery_calls=1):
        clock = [2000000000.0]
        class API:
            def call(self, method, path, **kwargs):
                if method == 'GET':
                    return next(p for p in rows if p['id'] == path.split('/')[-1])
                return {}
        with patch.object(sync, 'notion', return_value=API()), patch.object(
                sync, 'playlist_databases', return_value={'p': {'data_sources': [{'id': 'ds'}]}}) as discovery, patch.object(
                sync, 'pages', return_value=rows), patch.object(sync, 'transcript', side_effect=fetch) as fetched, patch.object(
                sync, 'write_body', side_effect=sync.TemporaryAPIError('test failure') if fail_body else None) as body, patch.object(
                backfill.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), patch.object(
                backfill.time, 'time', side_effect=lambda: clock[0]), patch.dict(sync.os.environ, {}, clear=True):
            result = backfill.run({'notion_parent_page_id': 'parent', 'transcript_retry_days': 7},
                                  2, 1, str(Path(directory) / 'cache.sqlite'), wait_on_block=wait_on_block)
            self.assertEqual(discovery.call_count, discovery_calls)
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
            result, fetches, writes = self.execute(directory, rows, lambda *args: self.fail('re-fetched'), discovery_calls=0)
            self.assertEqual((fetches, writes, result['saved_pages']), (0, 1, 1))

    def test_block_stops_other_videos_and_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [page('1', 'a'), page('2', 'b')]
            _, fetches, _ = self.execute(directory, rows, lambda *args: ('', 'Blocked', ''))
            self.assertEqual(fetches, 1)
            _, fetches, _ = self.execute(directory, rows, lambda *args: self.fail('ignored cooldown'), discovery_calls=0)
            self.assertEqual(fetches, 0)

    def test_budget_limits_unique_fetches(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = [page(str(i), str(i)) for i in range(4)]
            _, fetches, _ = self.execute(directory, rows, lambda *args: ('caption', 'Full', 'en'))
            self.assertEqual(fetches, 2)

    def test_block_waits_and_retries_same_video(self):
        with tempfile.TemporaryDirectory() as directory:
            result, fetches, writes = self.execute(directory, [page('1', 'a')],
                iter([('', 'Blocked', ''), ('caption', 'Full', 'en')]), wait_on_block=True)
            self.assertEqual((fetches, writes, result['saved_pages']), (2, 1, 1))
            with closing(sqlite3.connect(str(Path(directory) / 'cache.sqlite'))) as db:
                blocked = db.execute('SELECT blocked_until FROM settings').fetchone()[0]
                fetched = db.execute('SELECT fetched FROM delivered').fetchone()[0]
            self.assertGreaterEqual(fetched, blocked)

    def test_repeated_block_escalates_and_budget_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            _, fetches, _ = self.execute(directory, [page('1', 'a')],
                lambda *args: ('', 'Blocked', ''), wait_on_block=True)
            self.assertEqual(fetches, 2)
            with closing(sqlite3.connect(str(Path(directory) / 'cache.sqlite'))) as db:
                self.assertEqual(db.execute('SELECT strikes FROM throttle').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT blocked_until FROM settings').fetchone()[0], 2000005400)

    def test_restart_reuses_candidate_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            self.execute(directory, [page('1', 'a')], lambda *args: ('caption', 'Full', 'en'))
            self.execute(directory, [page('1', 'a')], lambda *args: self.fail('re-fetched'), discovery_calls=0)

    def test_legacy_cooldown_migrates_once(self):
        with tempfile.TemporaryDirectory() as directory:
            with closing(sqlite3.connect(str(Path(directory) / 'cache.sqlite'))) as db:
                db.execute('CREATE TABLE settings (scope TEXT PRIMARY KEY, blocked_until REAL)')
                db.execute('INSERT INTO settings VALUES (?, ?)', (json.dumps(['parent', ['en']], sort_keys=True), 2000604800))
                db.commit()
            self.execute(directory, [], lambda *args: self.fail('unexpected request'))
            with closing(sqlite3.connect(str(Path(directory) / 'cache.sqlite'))) as db:
                self.assertEqual(db.execute('SELECT blocked_until FROM settings').fetchone()[0], 2000001800)
