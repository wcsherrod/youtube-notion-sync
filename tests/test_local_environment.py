import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sync


class LocalEnvironmentTests(unittest.TestCase):
    def test_loads_bom_crlf_quotes_export_and_token_from_project(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / '.env').write_bytes(
                b'\xef\xbb\xbfNOTION_TOKEN="local token"\r\n'
                b'export NOTION_PARENT_PAGE_ID=page-id\r\n')
            (project / 'youtube-token.json').write_text(
                '{"refresh_token":"local"}', encoding='utf-8')
            outside = project / 'outside'
            outside.mkdir()
            with patch.dict(os.environ, {}, clear=True), patch.object(
                    sync.os, 'chdir') as chdir:
                sync.load_local_environment(project)
                self.assertEqual(os.environ['NOTION_TOKEN'], 'local token')
                self.assertEqual(os.environ['NOTION_PARENT_PAGE_ID'], 'page-id')
                self.assertEqual(
                    os.environ['YOUTUBE_TOKEN_JSON'], '{"refresh_token":"local"}')
                chdir.assert_called_once_with(project.resolve())

    def test_existing_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / '.env').write_text(
                'NOTION_TOKEN=local\nYOUTUBE_TOKEN_JSON=local-json\n',
                encoding='utf-8')
            (project / 'youtube-token.json').write_text(
                'file-json', encoding='utf-8')
            with patch.dict(os.environ, {
                    'NOTION_TOKEN': 'ci', 'YOUTUBE_TOKEN_JSON': 'ci-json'
                    }, clear=True), patch.object(sync.os, 'chdir'):
                sync.load_local_environment(project)
                self.assertEqual(os.environ['NOTION_TOKEN'], 'ci')
                self.assertEqual(os.environ['YOUTUBE_TOKEN_JSON'], 'ci-json')

    def test_rejects_malformed_lines_without_exposing_values(self):
        cases = {
            'missing equals': 'BROKEN',
            'invalid key': 'BAD-KEY=secret',
            'unterminated quote': 'TOKEN="secret',
        }
        for name, content in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                (project / '.env').write_text(content, encoding='utf-8')
                with patch.dict(os.environ, {}, clear=True), patch.object(
                        sync.os, 'chdir'):
                    with self.assertRaises(sync.SyncError) as caught:
                        sync.load_local_environment(project)
                self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
