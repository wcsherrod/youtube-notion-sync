import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, Mock
import sync

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'checkpoint.json'
        self.config = {'notion_parent_page_id':'parent', 'playlist_ids':[], 'checkpoint_file':str(self.path), 'transcript_budget':50}
        self.playlists = [{'id':v, 'snippet':{'title':v, 'channelId':'channel'}} for v in ('a','b','c')]
        self.stack.enter_context(patch.dict(sync.os.environ, {'GITHUB_ACTIONS':'', 'NOTION_PARENT_PAGE_ID':'', 'YOUTUBE_TOKEN_JSON':'{}'}))
        self.stack.enter_context(patch.object(sync.progress,'log'))
        self.stack.enter_context(patch.object(sync,'notion',return_value=Mock(base='url',headers={})))
        self.stack.enter_context(patch.object(sync,'youtube',return_value=Mock()))
        self.listing = self.stack.enter_context(patch.object(sync,'yt_list',side_effect=lambda *a,**k: iter(self.playlists)))
        self.stack.enter_context(patch.object(sync,'playlist_databases',return_value={v:{'id':'db-'+v} for v in ('a','b','c','d')}))
        self.stack.enter_context(patch.object(sync,'ensure_database',return_value='ds'))
        self.stack.enter_context(patch.object(sync,'ensure_gallery'))
        self.stack.enter_context(patch.object(sync,'report_results'))

    def test_conflicting_database_does_not_stop_other_playlists(self):
        def destination(api,parent,playlist,found):
            if playlist['id']=='b':
                raise sync.PlaylistDatabaseConflict('duplicate b')
            return 'ds'
        seen=[]
        with patch.object(sync,'ensure_database',side_effect=destination), patch.object(sync,'run_single',side_effect=lambda c,d,playlist,y: seen.append(playlist['id']) or {}):
            sync.run(self.config)
        self.assertEqual(seen,['a','c'])
        self.assertEqual(json.loads(self.path.read_text())['completed_playlist_ids'],['a','c'])

    def test_interruption_resume_and_next_full_pass(self):
        seen = []
        def fail(config, ds, playlist, y):
            seen.append(playlist['id'])
            if playlist['id']=='b': raise sync.SyncError('quotaExceeded')
            return {}
        with patch.object(sync,'run_single',side_effect=fail):
            with self.assertRaises(sync.SyncError): sync.run(self.config)
        self.assertEqual(seen,['a','b'])
        self.assertEqual(json.loads(self.path.read_text())['completed_playlist_ids'],['a'])
        seen.clear()
        def ok(config,ds,playlist,y):
            seen.append(playlist['id'])
            return {}
        with patch.object(sync,'run_single',side_effect=ok):
            sync.run(self.config)
            self.assertEqual(seen,['b','c'])
            self.assertFalse(self.path.exists())
            seen.clear()
            sync.run(self.config)
            self.assertEqual(seen,['a','b','c'])
        self.assertEqual(self.listing.call_count,3)

    def test_deferred_and_new_playlist(self):
        with patch.object(sync,'run_single',side_effect=lambda c,d,playlist,y: {'deferred_writes':int(playlist['id']=='b')}):
            sync.run(self.config)
        self.assertEqual(json.loads(self.path.read_text())['completed_playlist_ids'],['a','c'])
        self.playlists.reverse()
        self.playlists.append({'id':'d','snippet':{'title':'d','channelId':'channel'}})
        seen=[]
        with patch.object(sync,'run_single',side_effect=lambda c,d,playlist,y: seen.append(playlist['id']) or {}):
            sync.run(self.config)
        self.assertEqual(seen,['b','d'])

    def test_settings_change_restarts_full_pass(self):
        state=sync.load_checkpoint(self.path,'parent',['a','b','c'],sync.checkpoint_context(self.config,self.playlists))
        sync.mark_playlist_complete(self.path,state,'a')
        self.config['transcript_budget']=100
        seen=[]
        with patch.object(sync,'run_single',side_effect=lambda c,d,playlist,y: seen.append(playlist['id']) or {}): sync.run(self.config)
        self.assertEqual(seen,['a','b','c'])

    def test_corrupt_checkpoint_rejected(self):
        self.path.write_text('{"playlist_ids":"abc","completed_playlist_ids":[]}')
        with patch.object(sync,'run_single') as writer:
            with self.assertRaises(sync.SyncError): sync.run(self.config)
        writer.assert_not_called()

    def test_atomic_write_keeps_old_file_on_failure(self):
        sync.save_checkpoint(self.path,{'old':'state'})
        with patch.object(sync.os,'replace',side_effect=OSError()):
            with self.assertRaises(sync.SyncError): sync.save_checkpoint(self.path,{'new':'state'})
        self.assertEqual(json.loads(self.path.read_text()),{'old':'state'})

    def test_auth_prepared_for_every_request(self):
        creds=Mock()
        api=sync.API('https://www.googleapis.com/youtube/v3/',{},credentials=creds)
        response=Mock(ok=True)
        response.json.return_value={}
        with patch.object(sync.requests,'request',return_value=response),patch.object(sync.time,'sleep'):
            api.call('GET','playlists')
            api.call('GET','videos')
        self.assertEqual(creds.before_request.call_count,2)

class RemoteTests(unittest.TestCase):
    class Fake:
        def __init__(self): self.blocks={'parent':[],'cp':[]}; self.serial=0
        def call(self,method,path,**kw):
            if method=='GET': return {'results':copy.deepcopy(self.blocks[path.split('/')[1]]),'has_more':False}
            if method=='POST':
                self.blocks['parent'].append({'id':'cp','type':'child_page','child_page':{'title':sync.NotionCheckpoint.TITLE}})
                return {'id':'cp'}
            if method=='PATCH':
                block=copy.deepcopy(kw['json']['children'][0]); self.serial+=1; block['id']=str(self.serial)
                self.blocks[block['id']]=block['toggle'].pop('children')
                self.blocks['cp'].append(block)
                return {'results':[copy.deepcopy(block)]}
            if method=='DELETE':
                self.blocks['cp']=[b for b in self.blocks['cp'] if b['id']!=path.split('/')[1]]
                return {}
            raise AssertionError(method)

    def test_remote_resume_and_completion(self):
        api=self.Fake(); cp=sync.NotionCheckpoint(api,'parent')
        state={'playlist_ids':['a','b'],'completed_playlist_ids':['a']}
        cp.save(state)
        restored=sync.NotionCheckpoint(api,'parent')
        self.assertEqual(json.loads(restored.read_text()),state)
        restored.unlink()
        self.assertFalse(sync.NotionCheckpoint(api,'parent').exists())

    def test_long_snapshot_and_history(self):
        api=self.Fake(); cp=sync.NotionCheckpoint(api,'parent')
        state={'playlist_ids':['PL'+str(i).zfill(40) for i in range(83)]}
        for i in range(4): cp.save(state)
        self.assertEqual(len(api.blocks['cp']),2)
        self.assertEqual(json.loads(sync.NotionCheckpoint(api,'parent').read_text()),state)

    def test_partial_snapshot_rejected(self):
        api=self.Fake(); cp=sync.NotionCheckpoint(api,'parent'); cp.save({'saved':'state'})
        api.blocks[str(api.serial)]=[]
        with self.assertRaises(sync.SyncError): sync.NotionCheckpoint(api,'parent')
