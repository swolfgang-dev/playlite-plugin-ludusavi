import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QFormLayout, QMainWindow, QWidget
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ludusavi_under_test', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
PACKAGE = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = PACKAGE; SPEC.loader.exec_module(PACKAGE)
from ludusavi_under_test.backup import BackupEngine, resolve_context, cloud_busy
from ludusavi_under_test.plugin import Plugin, BackupQueue

APP = QApplication.instance() or QApplication([])


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / 'prefix'; (self.prefix / 'drive_c').mkdir(parents=True)
        self.config = self.root / 'config'; self.config.mkdir()
        self.original = {'roots': [{'store': 'otherWindows', 'path': '/mnt/vm/Windows'}],
                         'redirects': [{'source': '/host', 'target': '/vm'}],
                         'backup': {'path': str(self.root / 'backups'), 'retention': {'full': 5}},
                         'cloud': {'remote': {'GoogleDrive': {'id': 'test'}}, 'synchronize': True}}
        (self.config / 'config.yaml').write_text(yaml.safe_dump(self.original)); (self.config / 'manifest.yaml').write_text('{}')
        self.engine = BackupEngine({'config': str(self.config), 'state': str(self.root / 'state'), 'cloud': True})
        busy = patch('ludusavi_under_test.backup.cloud_busy', return_value=False)
        busy.start(); self.addCleanup(busy.stop)
        self.context = dict(prefix=str(self.prefix), directory=str(self.root / 'Games/Example'), name='Example', steam_id='42', exact_name=False)
        self.calls = []
        self.cloud_change = 'Same'; self.cloud_failure = False; self.no_files = False

    def execute(self, args, config, timeout=180):
        self.calls.append(args)
        snapshot = yaml.safe_load((config / 'config.yaml').read_text())
        self.assertEqual(snapshot['roots'][0], {'store': 'otherWine', 'path': str(self.prefix)})
        self.assertNotIn('otherWindows', [r['store'] for r in snapshot['roots']])
        self.assertEqual(snapshot['redirects'], [])
        self.assertEqual(snapshot['backup']['retention'], {'full': 5})
        if args[0] == 'find': return {'games': {'Example': {'score': 1}}}
        if args[0] == 'cloud':
            if '--preview' in args: return {'cloud': {'save': {'change': self.cloud_change}}}
            if self.cloud_failure: raise ValueError('Cloud unavailable')
            return {}
        files = {} if self.no_files else {str(self.prefix / 'drive_c/save.dat'): {'bytes': 10}}
        return {'games': {'Example': {'decision': 'Processed', 'files': files}}}

    def run_engine(self, preview=False):
        with patch.object(self.engine, 'execute', side_effect=self.execute):
            result = self.engine.run(self.context, preview=preview)
        self.assertEqual(yaml.safe_load((self.config / 'config.yaml').read_text()), self.original)
        return result

    def test_local_backup_precedes_cloud_upload_and_uses_only_one_game(self):
        result = self.run_engine()
        self.assertEqual(result['cloud'], 'Uploaded'); self.assertTrue(result['local_saved'])
        local = next(i for i, args in enumerate(self.calls) if args[0] == 'backup' and '--preview' not in args)
        upload = next(i for i, args in enumerate(self.calls) if args[0] == 'cloud' and '--preview' not in args)
        self.assertLess(local, upload)
        self.assertTrue(all(args[-2:] == ['--', 'Example'] for args in self.calls))
        self.assertIn('--no-cloud-sync', self.calls[local])

    def test_cloud_commands_wait_for_external_upload_before_backup(self):
        updates = []
        def status(message):
            updates.append(message)
            if message.startswith('Waiting'):
                self.assertFalse(any(args[0] in ('cloud', 'backup') for args in self.calls))
        with patch('ludusavi_under_test.backup.cloud_busy', side_effect=[True, True, False, False]), \
             patch('ludusavi_under_test.backup.time.sleep') as sleep, \
             patch.object(self.engine, 'execute', side_effect=self.execute) as execute:
            result = self.engine.run(self.context, status=status)
        self.assertEqual(result['cloud'], 'Uploaded')
        self.assertEqual(sleep.call_count, 2)
        self.assertIn('Waiting for the current Ludusavi upload to finish…', updates)
        upload = next(call for call in execute.call_args_list if call.args[0][0] == 'cloud' and '--force' in call.args[0])
        self.assertIsNone(upload.kwargs['timeout'])

    def test_busy_detection_tracks_ludusavi_transfers_and_ignores_unrelated_rclone(self):
        proc = self.root / 'proc'; proc.mkdir()
        def process(pid, name, args, parent):
            path = proc / str(pid); path.mkdir()
            (path / 'comm').write_text(name)
            (path / 'cmdline').write_bytes('\0'.join(args).encode())
            (path / 'status').write_text('PPid:\t' + str(parent) + '\n')
        process(1, 'ludusavi', ['ludusavi', 'gui'], 0)
        process(2, 'rclone', ['rclone', 'copy', 'source', 'target'], 1)
        self.assertTrue(cloud_busy(proc))
        (proc / '2/status').write_text('PPid:\t0\n')
        self.assertFalse(cloud_busy(proc))
        (proc / '1/cmdline').write_bytes(b'ludusavi\0cloud\0upload\0')
        self.assertTrue(cloud_busy(proc))

    def test_conflict_preserves_local_backup_without_cloud_upload(self):
        self.cloud_change = 'Different'; result = self.run_engine()
        self.assertTrue(result['local_saved']); self.assertEqual(result['cloud'], 'Needs attention')
        self.assertFalse(any(args[0] == 'cloud' and '--force' in args for args in self.calls))

    def test_cloud_failure_does_not_fail_local_backup(self):
        self.cloud_failure = True; result = self.run_engine()
        self.assertTrue(result['local_saved']); self.assertEqual(result['cloud'], 'Upload failed')

    def test_preview_never_writes_backups_or_contacts_cloud(self):
        result = self.run_engine(preview=True)
        self.assertEqual(result['status'], 'Preview')
        self.assertFalse(any(args[0] == 'cloud' for args in self.calls))
        self.assertTrue(all('--preview' in args for args in self.calls if args[0] == 'backup'))

    def test_no_saves_does_not_upload_or_report_local_success(self):
        self.no_files = True; result = self.run_engine()
        self.assertEqual(result['status'], 'No saves found'); self.assertFalse(result['local_saved'])
        self.assertFalse(any(args[0] == 'cloud' and '--force' in args for args in self.calls))

    def test_rejects_vm_and_missing_prefix_before_executing(self):
        for action in ({'Integration': 'VirtualMachine'}, {'Integration': 'LutrisIntegration', 'Prefix': '/mnt/vm/prefix'},
                       {'Integration': 'LutrisIntegration', 'Prefix': str(self.prefix), 'IsVM': True}):
            with self.subTest(action=action), self.assertRaises(ValueError):
                resolve_context({'Name': 'Example'}, action, [])

    def test_prefix_override_and_actual_action_are_preserved(self):
        context = resolve_context({'Name': 'Example', 'LudusaviBackup': {'name': 'Custom', 'prefix': str(self.prefix)}},
                                  {'Integration': 'SteamIntegration', 'GameId': '42'}, [])
        self.assertEqual(context['prefix'], str(self.prefix)); self.assertEqual(context['name'], 'Custom'); self.assertTrue(context['exact_name'])

    def test_automatic_backup_uses_captured_launch_not_current_other_action(self):
        plugin = Plugin(); controller = Mock(); game = {'Id': 'one', 'Name': 'Example'}
        action = {'Integration': 'LutrisIntegration', 'Prefix': str(self.prefix)}
        with patch.object(plugin, 'controller', return_value=controller), patch.object(plugin, 'options', return_value={'automatic': True}):
            plugin.after_launch(None, game, action)
            action['Prefix'] = '/different'
            plugin.after_game_stopped(None, game, action)
            self.assertEqual(controller.add.call_args.args[1]['Prefix'], str(self.prefix))
            plugin.after_game_stopped(None, game, action)
        controller.add.assert_called_once()

    def test_disabled_external_and_vm_sessions_do_not_queue(self):
        plugin = Plugin(); controller = Mock()
        with patch.object(plugin, 'controller', return_value=controller), patch.object(plugin, 'options', return_value={'automatic': True}):
            for mode, action in [('disabled', {'Integration': 'LutrisIntegration'}), ('default', {'Integration': 'VirtualMachine'})]:
                game = {'Id': 'one', 'Name': 'Example', 'LudusaviBackup': {'automatic': mode}}
                plugin.after_launch(None, game, action); plugin.after_game_stopped(None, game, action)
            plugin.after_game_stopped(None, {'Id': 'external'}, None)
        controller.add.assert_not_called()


class PluginUiTests(unittest.TestCase):
    def test_settings_cancel_and_save_are_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / 'settings.ini'), QSettings.Format.IniFormat)
            plugin = Plugin()
            with patch.object(plugin, 'settings', return_value=settings):
                widget = plugin.create_settings()
                widget.automatic.setChecked(False)
                self.assertTrue(plugin.options()['automatic'])
                plugin.save_settings(widget)
                self.assertFalse(plugin.options()['automatic'])
                widget.close()

    def test_per_game_overrides_are_collected_and_invalid_prefix_rejected(self):
        plugin = Plugin(); panel = QWidget()
        editor = SimpleNamespace(game={}, installation_form=QFormLayout(panel))
        plugin.augment_editor(editor)
        editor.ludusavi_confirmed.setChecked(True)
        editor.ludusavi_files.setPlainText('/changed/save')
        self.assertFalse(editor.ludusavi_confirmed.isChecked())
        editor.ludusavi_mode.setCurrentIndex(editor.ludusavi_mode.findData('disabled'))
        editor.ludusavi_name.setText('Manifest title')
        editor.ludusavi_files.setPlainText('/custom/save\n<winAppData>/Example/save')
        editor.ludusavi_registry.setPlainText('HKEY_CURRENT_USER/Software/Example')
        game = {}; plugin.collect_editor(editor, game)
        self.assertEqual(game['LudusaviBackup']['files'], ['/custom/save', '<winAppData>/Example/save'])
        self.assertEqual(game['LudusaviBackup']['registry'], ['HKEY_CURRENT_USER/Software/Example'])
        self.assertEqual(game['LudusaviBackup']['automatic'], 'disabled')
        self.assertEqual(game['LudusaviBackup']['name'], 'Manifest title')
        editor.ludusavi_prefix.setText('relative/path')
        with self.assertRaises(ValueError): plugin.collect_editor(editor, {})
        panel.close()

    def test_background_queue_is_serial_and_deduplicates_games(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); prefix = root / 'prefix'; (prefix / 'drive_c').mkdir(parents=True)
            window = QMainWindow(); window.game_providers = []
            queue = BackupQueue(window); tasks = []
            action = {'Integration': 'LutrisIntegration', 'Prefix': str(prefix)}
            settings = {'state': str(root / 'state')}
            pool = SimpleNamespace(start=tasks.append)
            with patch('ludusavi_under_test.plugin.QThreadPool.globalInstance', return_value=pool), patch('ludusavi_under_test.plugin.BackupEngine') as engine:
                engine.return_value.run.return_value = {'status': 'Local backup saved', 'cloud': 'Disabled'}
                queue.add({'Id': 'a', 'Name': 'A'}, action, settings)
                queue.add({'Id': 'b', 'Name': 'B'}, action, settings)
                queue.add({'Id': 'b', 'Name': 'B'}, action, settings)
                self.assertEqual(len(tasks), 1); self.assertEqual(len(queue.pending), 1)
                self.assertEqual(queue.results['b']['status'], 'Backup queued')
                queue.update_progress('Waiting for the current Ludusavi upload to finish…')
                self.assertEqual(queue.results['a']['cloud'], 'Waiting')
                tasks[0].run()
                self.assertEqual(len(tasks), 2); self.assertEqual(queue.active[0]['Id'], 'b')
                tasks[1].run()
                self.assertIsNone(queue.active); self.assertEqual(set(queue.results), {'a', 'b'})
            window.close()
