import test_backup
import unittest
from ludusavi_under_test.definitions import sync_definitions, read_definition
from unittest.mock import patch
import yaml


class DefinitionTests(unittest.TestCase):
    setUp = test_backup.BackupTests.setUp
    execute = test_backup.BackupTests.execute

    def test_sync_preserves_edits_additional_paths_and_other_game_mappings(self):
        manifest = {'Canonical': {'steam': {'id': 42}, 'files': {'<winAppData>/Official/save': {}}}}
        (self.config / 'manifest.yaml').write_text(yaml.safe_dump(manifest))
        data = dict(self.original, customGames=[{'name': 'Canonical', 'files': ['/extra/save']}])
        (self.config / 'config.yaml').write_text(yaml.safe_dump(data))
        game = {'Id': 'one', 'Name': 'Different title', 'MetadataIds': {'SteamMetadata': 42},
                'PlayActions': [{'Integration': 'LutrisIntegration', 'Prefix': str(self.prefix)}]}
        settings = self.engine.settings
        preview = {'games': {'Canonical': {'files': {'/extra/save': {}}}, 'Second': {'files': {'/other/save': {}}}}}
        with patch('ludusavi_under_test.definitions.BackupEngine.execute', return_value=preview):
            result = sync_definitions([game], [], settings)
            self.assertEqual(result['updated'][0]['name'], 'Canonical')
            self.assertEqual(read_definition(game, settings)['files'], ['<winAppData>/Official/save', '/extra/save'])
            second = dict(game, Id='two', Name='Second', MetadataIds={}, LudusaviBackup={'files': ['/other/save'], 'registry': []})
            sync_definitions([second], [], settings)
            self.assertEqual(read_definition(game, settings)['name'], 'Canonical')
            game['LudusaviBackup'] = {'files': ['/edited/save'], 'registry': []}
            sync_definitions([game], [], settings)
            self.assertEqual(read_definition(game, settings)['files'], ['/edited/save'])
            self.assertEqual(read_definition(game, settings)['integration'], 'override')

    def test_new_definition_without_save_data_is_skipped(self):
        (self.config / 'manifest.yaml').write_text(yaml.safe_dump({'Example': {'files': {'/absent/save': {}}}}))
        game = {'Id': 'one', 'Name': 'Example', 'PlayActions': [{'Integration': 'LutrisIntegration', 'Prefix': str(self.prefix)}]}
        with patch('ludusavi_under_test.definitions.BackupEngine.execute', return_value={'games': {'Example': {'files': {}}}}):
            result = sync_definitions([game], [], self.engine.settings)
        self.assertEqual(result['skipped'], ['Example'])
        self.assertEqual(yaml.safe_load((self.config / 'config.yaml').read_text())['customGames'], [])

    def test_backup_uses_saved_rules_without_changing_global_config(self):
        self.context.update(files=['/edited/save'], registry=['HKEY_CURRENT_USER/Software/Example'], exact_name=True)
        def execute(args, config, timeout=180):
            custom = yaml.safe_load((config / 'config.yaml').read_text())['customGames'][0]
            self.assertEqual(custom['files'], ['/edited/save'])
            self.assertEqual(custom['integration'], 'override')
            return self.execute(args, config, timeout)
        with patch.object(self.engine, 'execute', side_effect=execute):
            self.engine.run(self.context, preview=True)
        self.assertEqual(yaml.safe_load((self.config / 'config.yaml').read_text()), self.original)
