import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sync

class CheckpointTests(unittest.TestCase):
    def test_absent_checkpoint_starts_full_scan_and_resume_skips_completed(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sync.progress, 'log'):
            path = Path(folder) / 'checkpoint.json'
            state = sync.load_checkpoint(path, 'parent', ['a', 'b', 'c'])
            self.assertEqual(state['completed_playlist_ids'], [])
            self.assertTrue(path.exists())
            sync.mark_playlist_complete(path, state, 'a')
            resumed = sync.load_checkpoint(path, 'parent', ['a', 'b', 'c'])
            remaining = [p for p in resumed['playlist_ids']
                         if p not in set(resumed['completed_playlist_ids'])]
            self.assertEqual(remaining, ['b', 'c'])

    def test_new_playlist_is_appended_and_removed_one_does_not_break_resume(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sync.progress, 'log'):
            path = Path(folder) / 'checkpoint.json'
            state = sync.load_checkpoint(path, 'parent', ['a', 'b'])
            sync.mark_playlist_complete(path, state, 'a')
            resumed = sync.load_checkpoint(path, 'parent', ['a', 'c'])
            self.assertEqual(resumed['playlist_ids'], ['a', 'c'])
            self.assertEqual(resumed['completed_playlist_ids'], ['a'])

    def test_checkpoint_write_is_valid_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'checkpoint.json'
            sync.save_checkpoint(path, {'version': 1})
            self.assertEqual(json.loads(path.read_text()), {'version': 1})
            self.assertFalse(Path(str(path) + '.tmp').exists())

    def test_wrong_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sync.progress, 'log'):
            path = Path(folder) / 'checkpoint.json'
            sync.load_checkpoint(path, 'first', ['a'])
            with self.assertRaises(sync.SyncError):
                sync.load_checkpoint(path, 'second', ['a'])

    def test_supplied_playlist_avoids_relisting_all_playlists(self):
        playlist = {'id': 'p', 'snippet': {'title': 'Test'}}
        class Fake:
            def call(self, method, path, **kwargs):
                if path == 'videos':
                    return {'items': []}
                raise AssertionError(path)
        def listing(api, resource, **kwargs):
            self.assertNotEqual(resource, 'playlists')
            return []
        with patch.object(sync, 'notion', return_value=Fake()),              patch.object(sync, 'pages', return_value=[]),              patch.object(sync, 'yt_list', side_effect=listing),              patch.object(sync.progress, 'log'):
            result = sync.run_single(
                {'playlist_ids': ['p'], 'transcripts': 'off',
                 'transcript_budget': 0, 'transcript_retry_days': 7},
                'ds', playlist=playlist, y=Fake())
        self.assertEqual(result['entries_scanned'], 0)

if __name__ == '__main__':
    unittest.main()
