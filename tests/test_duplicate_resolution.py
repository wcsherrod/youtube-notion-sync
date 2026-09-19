import unittest
from unittest.mock import patch
import sync

def saved(page_id, item_id, status='', complete=False):
    return {'id':page_id,'properties':{
        'Item ID':sync.rich(item_id), 'Transcript status':sync.rich(status),
        'Content hash':sync.rich('saved' if complete else '')}}

class DuplicateResolutionTests(unittest.TestCase):
    def test_prefers_full_transcript_without_mutating_copies(self):
        import copy
        rows=[saved('a','item','Pending',True),saved('b','item','Full',True)]
        before=copy.deepcopy(rows)
        with patch.object(sync.progress,'log'):
            indexed,extra=sync.index_existing(rows)
        self.assertEqual(indexed['item']['id'],'b')
        self.assertEqual(extra,1)
        self.assertEqual(rows,before)

    def test_prefers_complete_copy_and_stable_order(self):
        rows=[saved('a','item','',False),saved('b','item','',True)]
        self.assertEqual(sync.preferred_copy(rows)['id'],'b')
        self.assertEqual(sync.preferred_copy(list(reversed(rows)))['id'],'b')

    def test_distinct_playlist_item_ids_remain_distinct(self):
        rows=[saved('a','first'),saved('b','second')]
        indexed,extra=sync.index_existing(rows)
        self.assertEqual(len(indexed),2)
        self.assertEqual(extra,0)

    def test_error_reporting_is_specific_and_sanitized(self):
        self.assertEqual(sync.error_message(sync.SyncError('API POST views returned HTTP 400')),
                         'API POST views returned HTTP 400')
        self.assertIn('Missing NOTION_TOKEN',sync.error_message(KeyError('NOTION_TOKEN')))
        self.assertNotIn('SECRET',sync.error_message(ValueError('SECRET')))
        self.assertNotIn('credentials',sync.error_message(sync.SyncError('Duplicate playlist databases')))

if __name__ == '__main__':
    unittest.main()


class DatabaseConflictTests(unittest.TestCase):
    def test_discovery_preserves_duplicates_without_writes(self):
        from unittest.mock import Mock, patch
        def db(id, playlist):
            return {'id':id,'description':sync.rt(sync.DB_PREFIX+playlist),'data_sources':[{'id':'ds-'+id}]}
        copies = [db('one','p'),db('two','p'),db('three','q')]
        api=Mock()
        api.call.side_effect=lambda method,path: next(d for d in copies if path=='databases/'+d['id'])
        blocks=[{'type':'child_database','id':d['id']} for d in copies]
        with patch.object(sync,'children',return_value=blocks),patch.object(sync.progress,'log'):
            found=sync.playlist_databases(api,'parent',defer_conflicts=True)
        self.assertIsInstance(found['p'],sync.PlaylistDatabaseConflict)
        self.assertEqual(found['q']['id'],'three')
        self.assertIn('https://www.notion.so/one',str(found['p']))
        api.reset_mock()
        with self.assertRaises(sync.PlaylistDatabaseConflict):
            sync.ensure_database(api,'parent',{'id':'p','snippet':{'title':'p'}},found)
        api.call.assert_not_called()
        with patch.object(sync,'children',return_value=blocks):
            with self.assertRaises(sync.PlaylistDatabaseConflict):sync.playlist_databases(api,'parent')

    def test_repeated_same_database_is_not_a_conflict(self):
        from unittest.mock import Mock, patch
        db={'id':'one','description':sync.rt(sync.DB_PREFIX+'p'),'data_sources':[{'id':'ds'}]}
        api=Mock(); api.call.return_value=db
        with patch.object(sync,'children',return_value=[{'type':'child_database','id':'one'}]*2):
            self.assertEqual(sync.playlist_databases(api,'parent')['p'],db)
