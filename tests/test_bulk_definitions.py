import json
import test_backup
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from ludusavi_under_test.bulk_definitions import apply_overrides


class BulkDefinitionTests(unittest.TestCase):
    def test_selected_fields_merge_with_latest_library_and_keep_other_games(self):
        with TemporaryDirectory() as temporary:
            data = Path(temporary)
            games = [{'Id': 'one', 'Name': 'Renamed since opening', 'LudusaviBackup': {'name': 'Canonical', 'prefix': '/distinct/prefix', 'files': ['/old/save']}},
                     {'Id': 'two', 'Name': 'Untouched'}]
            (data / 'library.json').write_text(json.dumps(games))
            result = apply_overrides(data, {'one': {'automatic': 'disabled'}})
            self.assertEqual(result[0]['Name'], games[0]['Name'])
            self.assertEqual(result[0]['LudusaviBackup']['files'], ['/old/save'])
            self.assertEqual(result[0]['LudusaviBackup']['prefix'], '/distinct/prefix')
            self.assertEqual(result[1], games[1])
            self.assertEqual(json.loads((data / 'library.json').read_text()), result)

    def test_missing_selected_game_prevents_all_changes(self):
        with TemporaryDirectory() as temporary:
            data = Path(temporary); path = data / 'library.json'
            original = '[{"Id": "one", "Name": "Example"}]'; path.write_text(original)
            with self.assertRaises(ValueError): apply_overrides(data, {'one': {'files': []}, 'missing': {'files': []}})
            self.assertEqual(path.read_text(), original)

    def test_registry_only_edit_preserves_file_rules_and_canonical_title(self):
        from unittest.mock import Mock, patch
        from PyQt6.QtWidgets import QMainWindow, QCheckBox, QPlainTextEdit
        from ludusavi_under_test.bulk_definitions import edit_definitions
        with TemporaryDirectory() as temporary:
            data = Path(temporary)
            games = [{'Id': 'one', 'Name': 'Display title'}]
            (data / 'library.json').write_text(json.dumps(games))
            window = QMainWindow(); window.data = data; window.games = games; window.refresh_library = Mock()
            plugin = Mock()
            def accept(dialog):
                next(box for box in dialog.findChildren(QCheckBox) if box.text() == 'Replace registry rules').setChecked(True)
                dialog.findChildren(QPlainTextEdit)[1].setPlainText('HKEY_CURRENT_USER/Software/Example')
                self.assertIn('registry:', dialog.findChildren(QPlainTextEdit)[2].toPlainText())
                return dialog.DialogCode.Accepted
            definition = {'name': 'Canonical title', 'files': ['/known/save'], 'registry': []}
            with patch('ludusavi_under_test.bulk_definitions.run_dialog', side_effect=accept), patch('ludusavi_under_test.bulk_definitions.read_definition', return_value=definition):
                edit_definitions(plugin, window, games)
            saved = window.games[0]['LudusaviBackup']
            self.assertEqual(saved['files'], ['/known/save'])
            self.assertEqual(saved['name'], 'Canonical title')
            self.assertEqual(saved['registry'], ['HKEY_CURRENT_USER/Software/Example'])
            plugin.sync_definitions.assert_called_once()
            window.close()
