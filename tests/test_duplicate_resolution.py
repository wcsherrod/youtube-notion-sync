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
