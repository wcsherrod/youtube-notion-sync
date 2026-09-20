import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import merge_duplicates as m
import sync


def page(pid, ds, item, status='Pending'):
    props = {k:dict(type='rich_text', **sync.rich(v)) for k,v in {
        'Item ID':item,'Playlist ID':'pl','Description':'description',
        'Transcript status':status,'Content hash':'hash'}.items()}
    props['Name']={'type':'title','title':sync.rt(pid)}
    return {'id':pid,'parent':{'data_source_id':ds},'last_edited_time':'before','properties':props}


class Fake:
    def __init__(self):
        self.dbs={k:{'id':k,'parent':{'page_id':'root'},'title':sync.rt(k),
                      'description':sync.rt(sync.DB_PREFIX+'pl'),'data_sources':[{'id':k+'s'}]}
                  for k in ('a','b')}
        self.rows={'one':page('one','as','same'), 'two':page('two','as','unique'),
                   'three':page('three','bs','same','Full')}
        self.schemas={k:{n:{'type':p['type']} for n,p in self.rows['one']['properties'].items()}
                      for k in ('as','bs')}
        self.writes=[];self.drop=False
    def children(self, api, parent):
        if parent=='root':return [{'id':k,'type':'child_database'} for k in self.dbs]
        return [{'id':parent+'-body','type':'paragraph'}]
    def pages(self, api, ds):
        return copy.deepcopy([p for p in self.rows.values() if p['parent']['data_source_id']==ds])
    def call(self, method, path, **kw):
        bits=path.split('/'); kind,pid=bits[:2]
        if method=='GET':
            if kind=='pages':return copy.deepcopy(self.rows[pid])
            if kind=='databases':return copy.deepcopy(self.dbs[pid])
            return {'properties':copy.deepcopy(self.schemas[pid])}
        self.writes.append((method,path))
        body=kw['json']
        if method=='POST':
            self.rows[pid]['parent']=body['parent']
            if self.drop:self.drop=False;raise sync.TemporaryAPIError('lost response')
        elif kind=='pages':
            for name,value in body['properties'].items():
                self.rows[pid]['properties'][name]={'type':next(iter(value)),**copy.deepcopy(value)}
        elif kind=='databases':self.dbs[pid].update(body)
        elif kind=='data_sources':
            for name,value in body['properties'].items():self.schemas[pid][name]={'type':next(iter(value))}
        return {}


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.j=m.Journal(Path(self.tmp.name)/'journal.sqlite');self.addCleanup(self.j.close)
        self.api=Fake()
        for name in ('children','pages'):
            p=patch.object(sync,name,getattr(self.api,name));p.start();self.addCleanup(p.stop)
        self.config={'checkpoint_file':str(Path(self.tmp.name)/'missing.json')}
    def plan(self):m.plan(self.api,'root',self.j,Path(self.tmp.name)/'report.md')
    def test_plan_read_only_and_full_wins(self):
        self.plan();self.assertEqual(self.api.writes,[])
        pair=self.j.get('pair:pl')
        self.assertEqual(pair['target']['id'],'a')
        self.assertEqual({r['page']['id'] for r in pair['winners']},{'two','three'})
    def test_merge_preserves_all_pages_and_body_ids(self):
        self.plan();m.apply(self.api,'root',self.config,self.j)
        self.assertEqual({p['id'] for p in self.api.pages(None,'as')},{'two','three'})
        self.assertEqual(set(self.api.rows),{'one','two','three'})
        self.assertTrue(m.marker(self.api.dbs['b']).startswith(m.BACKUP_PREFIX))
        self.assertIn('one',self.api.rows['three']['properties']['Merge copies']['rich_text'][0]['text']['link']['url'])
        self.assertTrue(self.j.get('finished:pl'))
    def test_resume_after_move_response_lost(self):
        self.plan();self.api.drop=True
        with patch.object(m.time, 'monotonic', side_effect=range(0,10000,400)):
            m.apply(self.api,'root',self.config,self.j)
        self.assertEqual(self.api.writes.count(('POST','pages/one/move')),1)
        self.assertTrue(self.j.get('finished:pl'))
    def test_recovery_does_not_retry_validation_errors(self):
        from unittest.mock import Mock
        operation=Mock(side_effect=sync.NotionValidationError('bad payload'))
        with self.assertRaises(sync.NotionValidationError):m.recover(operation,'test')
        self.assertEqual(operation.call_count,1)
    def test_old_plan_accepted(self):
        self.plan()
        saved=self.j.get('plan');saved['build']='2026-09-19-merge-v1';self.j.set('plan',saved)
        m.apply(self.api,'root',self.config,self.j)
        self.assertTrue(self.j.get('finished:pl'))
    def test_changed_page_rejected(self):
        self.plan();self.api.rows['one']['last_edited_time']='after'
        with self.assertRaisesRegex(sync.SyncError,'changed since comparison'):m.apply(self.api,'root',self.config,self.j)
        self.assertFalse(any(method=='POST' for method,path in self.api.writes))
    def test_new_row_rejected_before_writes(self):
        self.plan();self.api.rows['new']=page('new','as','new')
        with self.assertRaisesRegex(sync.SyncError,'membership changed'):m.apply(self.api,'root',self.config,self.j)
        self.assertEqual(self.api.writes,[])
    def test_checkpoint_only_invalidates_merged_playlist(self):
        path=Path(self.config['checkpoint_file'])
        path.write_text(json.dumps({'parent_page_id':'root','playlist_ids':['pl','other'],
            'completed_playlist_ids':['pl','other'],'pass_id':'keep','playlist_generations':{'other':4}}))
        m.reset_local_checkpoint(self.config,'root','pl')
        state=json.loads(path.read_text())
        self.assertEqual(state['completed_playlist_ids'],['other'])
        self.assertEqual(state['playlist_generations'],{'pl':1,'other':4})
        self.assertEqual(state['pass_id'],'keep')
    def test_preserve_formatted_text(self):
        p=page('x','as','x');p['properties']['Description']['rich_text'][0]['annotations']={'bold':True}
        self.assertTrue(m.writable(p)['Description']['rich_text'][0]['annotations']['bold'])
    def test_unsupported_custom_properties_rejected(self):
        p=page('x','as','x');p['properties']['Custom']={'type':'relation','relation':[]}
        with self.assertRaises(sync.SyncError):m.writable(p)

if __name__=='__main__':unittest.main()
