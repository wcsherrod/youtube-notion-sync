import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sync


class ValidationRecoveryTests(unittest.TestCase):
    def test_unicode_chunks_preserve_full_text(self):
        text = 'a' * 1799 + '\U0001f600' * 2500 + 'end'
        chunks = sync.rt(text)
        self.assertEqual(''.join(c['text']['content'] for c in chunks), text)
        self.assertTrue(all(len(c['text']['content'].encode('utf-16-le')) // 2 <= 1800 for c in chunks))

    def test_validation_message_and_no_blind_retry(self):
        response = Mock(ok=False, status_code=400)
        response.json.return_value = {'code': 'validation_error',
            'message': 'body.properties.Description: too long\nBearer private-token'}
        api = sync.API('https://api.notion.com/v1/', {'Authorization': 'Bearer private-token'})
        with patch.object(sync.requests, 'request', return_value=response) as request, patch.object(sync.time, 'sleep'):
            with self.assertRaises(sync.NotionValidationError) as caught:
                api.call('POST', 'pages', json={})
        self.assertIn('body.properties.Description: too long', str(caught.exception))
        self.assertNotIn('private-token', str(caught.exception))
        self.assertEqual(request.call_count, 1)

    def test_notion_400_without_expected_code_still_defers(self):
        for body in ({'code': 'unexpected', 'message': 'Invalid field'}, {}, []):
            response = Mock(ok=False, status_code=400)
            response.json.return_value = body
            with patch.object(sync.requests, 'request', return_value=response), patch.object(sync.time, 'sleep'):
                with self.assertRaises(sync.NotionValidationError):
                    sync.API('https://api.notion.com/v1/', {}).call('POST', 'pages', json={})

    def test_auth_error_is_not_deferred_as_validation(self):
        response = Mock(ok=False, status_code=401)
        response.json.return_value = {'code': 'unauthorized'}
        with patch.object(sync.requests, 'request', return_value=response), patch.object(sync.time, 'sleep'):
            with self.assertRaises(sync.SyncError) as caught:
                sync.API('https://api.notion.com/v1/', {}).call('POST', 'pages', json={})
        self.assertNotIsInstance(caught.exception, sync.NotionValidationError)

    def test_bad_video_continues_and_retries_after_reopening_checkpoint(self):
        for stage in ('create', 'body', 'finish'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
                cp_path = Path(folder) / 'checkpoint.sqlite'
                cp = sync.VideoCheckpoint(cp_path, 'pass', 'p', 'ds')
                playlist = {'id': 'p', 'snippet': {'title': 'Test'}}
                items = [{'id': str(i), 'snippet': {'title': 'Video', 'position': i},
                          'contentDetails': {'videoId': str(i)}} for i in range(2)]
                saved = {}
                creates = []
                fail = True
                def call(method, path, **kwargs):
                    props = kwargs.get('json', {}).get('properties', {})
                    if path == 'videos':
                        return {'items': []}
                    if method == 'POST' and path == 'pages':
                        key = sync.plain({'properties': props}, 'Item ID')
                        if fail and stage == 'create' and key == '0':
                            raise sync.NotionValidationError('bad create')
                        creates.append(key)
                        saved[key] = {'id': 'page' + key, 'properties': props.copy()}
                        return saved[key]
                    if method == 'PATCH' and path.startswith('pages/'):
                        key = path[-1]
                        if fail and stage == 'finish' and key == '0' and sync.plain({'properties': props}, 'Content hash'):
                            raise sync.NotionValidationError('bad finish')
                        saved[key]['properties'].update(props)
                    return {}
                api = Mock()
                api.call.side_effect = call
                def body(api, page_id, text):
                    if fail and stage == 'body' and page_id == 'page0':
                        raise sync.NotionValidationError('bad body')
                stack.enter_context(patch.object(sync, 'notion', return_value=api))
                stack.enter_context(patch.object(sync, 'pages', side_effect=lambda *a: list(saved.values())))
                stack.enter_context(patch.object(sync, 'yt_list', return_value=items))
                stack.enter_context(patch.object(sync, 'write_body', side_effect=body))
                captions = stack.enter_context(patch.object(sync, 'transcript', return_value=('complete transcript', 'Full', 'en')))
                stack.enter_context(patch.object(sync.time, 'sleep'))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                config = {'transcripts': 'best-effort', 'transcript_retry_days': 7,
                          'transcript_budget': 50, '_video_checkpoint': cp}
                result = sync.run_single(config, 'ds', playlist, api)
                self.assertEqual(result['deferred_writes'], 1)
                self.assertEqual(result['validation_errors'], 1)
                self.assertEqual(result['saved_transcripts'], 1)
                self.assertIsNone(cp.get('done:0'))
                self.assertTrue(cp.get('done:1'))
                self.assertEqual(cp.get('deferred:0')['video_id'], '0')
                cp.close()
                fail = False
                cp = sync.VideoCheckpoint(cp_path, 'pass', 'p', 'ds')
                config['_video_checkpoint'] = cp
                result = sync.run_single(config, 'ds', playlist, api)
                self.assertEqual(result['checkpoint_skipped'], 1)
                self.assertTrue(cp.get('done:0'))
                self.assertIsNone(cp.get('deferred:0'))
                self.assertEqual(captions.call_count, 2)  # cached caption reused on retry
                self.assertEqual(creates.count('0'), 1)  # saved partial page updated, not recreated
                cp.close()
