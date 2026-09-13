import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sona.api import AppApi
from sona.manuscript_export import export_txt, manuscript_filename


class ManuscriptExportTests(unittest.TestCase):
    def test_export_preserves_chinese_paragraphs_and_title(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / '文稿.txt'
            manuscript = {'title': '散步记录', 'body': '第一段，中文。\n\n第二段 🌳。'}
            export_txt(manuscript, destination)
            self.assertEqual(destination.read_bytes().decode('utf-8'),
                             '散步记录\n\n第一段，中文。\n\n第二段 🌳。\n')

    def test_failed_write_preserves_existing_file_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / '文稿.txt'
            destination.write_bytes(b'previous export')
            stream = Mock()
            stream.name = str(Path(directory) / '.sona-manuscript-test')
            Path(stream.name).touch()
            stream.write.side_effect = OSError('disk full')
            with patch('sona.manuscript_export.tempfile.NamedTemporaryFile') as temporary:
                temporary.return_value.__enter__.return_value = stream
                with self.assertRaisesRegex(ValueError, '文稿保存失败'):
                    export_txt({'title': '标题', 'body': '正文'}, destination)
            self.assertEqual(destination.read_bytes(), b'previous export')
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_default_filename_handles_path_characters_and_windows_reserved_names(self):
        for title, expected in (
            ('标题', '标题.txt'), (' ../标题:问题? ', '_标题_问题_.txt'),
            ('CON', '_CON.txt'), ('LPT1.notes', '_LPT1.notes.txt'), ('...', '文稿.txt'),
        ):
            with self.subTest(title=title):
                self.assertEqual(manuscript_filename(title), expected)

    def test_api_exports_saved_result_and_propagates_cancel_without_ai_calls(self):
        manuscripts = Mock()
        manuscript = {'id': 'saved', 'title': '标题', 'body': '正文'}
        manuscripts.repository.result.return_value = manuscript
        exporter = Mock(return_value=False)
        api = AppApi(Mock(), Mock(), Mock(), manuscripts=manuscripts, manuscript_export=exporter)
        self.assertFalse(api.export_manuscript('saved'))
        exporter.assert_called_once_with(manuscript)
        manuscripts.repository.result.assert_called_once_with('saved')
        manuscripts.create.assert_not_called()
        manuscripts.retry.assert_not_called()

        exporter.reset_mock()
        manuscripts.repository.result.side_effect = ValueError('文稿尚未完成或已删除。')
        with self.assertRaisesRegex(ValueError, '尚未完成'):
            api.export_manuscript('missing')
        exporter.assert_not_called()
