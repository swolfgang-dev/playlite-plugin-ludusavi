import test_backup
import unittest
from ludusavi_under_test.definitions import sync_definitions, read_definition, register_detected_locations
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

    def test_definition_without_save_data_is_kept_for_future_saves(self):
        (self.config / 'manifest.yaml').write_text(yaml.safe_dump({'Example': {'files': {'/absent/save': {}}}}))
        game = {'Id': 'one', 'Name': 'Example', 'PlayActions': [{'Integration': 'LutrisIntegration', 'Prefix': str(self.prefix)}]}
        with patch('ludusavi_under_test.definitions.BackupEngine.execute', return_value={'games': {'Example': {'files': {}}}}):
            result = sync_definitions([game], [], self.engine.settings)
        self.assertEqual(result['skipped'], [])
        self.assertEqual(yaml.safe_load((self.config / 'config.yaml').read_text())['customGames'][0]['files'], ['/absent/save'])

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


class DetectedLocationTests(unittest.TestCase):
    setUp = test_backup.BackupTests.setUp
    def test_register_extends_manifest_and_preserves_existing_rules(self):
        root = self.prefix / 'drive_c/users/steamuser/AppData/Roaming/GSE Saves/42'
        root.mkdir(parents=True)
        (self.config / 'manifest.yaml').write_text(yaml.safe_dump({'Canonical': {'steam': {'id': 42}}}))
        data = dict(self.original, customGames=[{'name': 'Canonical', 'integration': 'extend', 'files': ['/manual/save']}])
        (self.config / 'config.yaml').write_text(yaml.safe_dump(data))
        context = {'prefix': str(self.prefix), 'directory': '', 'steam_id': '42'}
        with patch('ludusavi_under_test.definitions.BackupEngine.execute', return_value={}):
            title = register_detected_locations({'Id': 'one', 'Name': 'Example', 'MetadataIds': {'SteamMetadata': 42}}, [context], self.engine.settings)
        self.assertEqual(title, 'Canonical')
        result = yaml.safe_load((self.config / 'config.yaml').read_text())
        self.assertCountEqual(result['customGames'][0]['files'], ['/manual/save', str(root)])
        self.assertEqual(result['customGames'][0]['integration'], 'override')
        self.assertEqual(result['cloud'], self.original['cloud'])


class ManagedLifecycleTests(unittest.TestCase):
    setUp = test_backup.BackupTests.setUp

    def test_manifest_updates_replace_generated_rules_and_remove_only_owned_entries(self):
        manifest = {'Example': {'files': {'/old/save': {}}}}
        (self.config / 'manifest.yaml').write_text(yaml.safe_dump(manifest))
        game = {'Id': 'one', 'Name': 'Example', 'PlayActions': [{'Integration': 'LutrisIntegration', 'Prefix': str(self.prefix)}]}
        with patch('ludusavi_under_test.definitions.BackupEngine.execute', return_value={}):
            sync_definitions([game], [], self.engine.settings, reconcile=True)
            manifest['Example']['files'] = {'/new/save': {}}
            (self.config / 'manifest.yaml').write_text(yaml.safe_dump(manifest))
            sync_definitions([game], [], self.engine.settings, reconcile=True)
            self.assertEqual(read_definition(game, self.engine.settings)['files'], ['/new/save'])
            config = yaml.safe_load((self.config / 'config.yaml').read_text())
            config['customGames'].append({'name': 'Unrelated', 'files': ['/personal/save']})
            (self.config / 'config.yaml').write_text(yaml.safe_dump(config))
            sync_definitions([], [], self.engine.settings, reconcile=True)
            self.assertEqual(yaml.safe_load((self.config / 'config.yaml').read_text())['customGames'], [{'name': 'Unrelated', 'files': ['/personal/save']}])

    def test_changed_prefix_replaces_old_prefix(self):
        (self.config / 'manifest.yaml').write_text(yaml.safe_dump({'Example': {'files': {'/save': {}}}}))
        game = {'Id': 'one', 'Name': 'Example', 'PlayActions': [{'Integration': 'LutrisIntegration', 'Prefix': str(self.prefix)}]}
        replacement = self.root / 'new-prefix'; (replacement / 'drive_c').mkdir(parents=True)
        with patch('ludusavi_under_test.definitions.BackupEngine.execute', return_value={}):
            sync_definitions([game], [], self.engine.settings)
            game['PlayActions'][0]['Prefix'] = str(replacement)
            sync_definitions([game], [], self.engine.settings)
        self.assertEqual(read_definition(game, self.engine.settings)['winePrefix'], [str(replacement)])


class RuleSourceTests(unittest.TestCase):
    setUp = test_backup.BackupTests.setUp

    def test_saved_rule_sources_distinguish_manifest_manual_and_emulator(self):
        from ludusavi_under_test.definitions import rule_sources
        (self.config / 'manifest.yaml').write_text(yaml.safe_dump({'Example': {'files': {'/official/save': {}}}}))
        game={'Name':'Example','LudusaviBackup':{'files':['/manual/save']}}
        definition={'name':'Example','files':['/official/save','/manual/save','/codex/save','/goldberg/save','/other/save']}
        with patch('ludusavi_under_test.definitions.emulator_save_paths',return_value=['/codex/save']), patch('ludusavi_under_test.definitions.gse_save_paths',return_value=['/goldberg/save']):
            rows=rule_sources(game,definition,self.engine.settings,self.context)
        self.assertEqual([row[2] for row in rows],['Ludusavi manifest','Manual override','CODEX configuration','Goldberg save directory','Custom definition'])
