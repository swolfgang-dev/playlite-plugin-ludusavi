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
from ludusavi_under_test.backup import BackupEngine, resolve_context, cloud_busy, emulator_save_paths, gse_save_paths
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

    def test_flatpak_access_is_read_only_for_scans_and_writable_for_restore_gui(self):
        self.engine.command = ['flatpak', 'run', 'com.github.mtkennerly.ludusavi']
        paths = [str(self.prefix), str(self.prefix)]
        scan = self.engine.access_command(paths)
        restore = self.engine.access_command(paths, writable=True)
        self.assertEqual(scan[2], '--filesystem=' + str(self.prefix) + ':ro')
        self.assertEqual(restore[2], '--filesystem=' + str(self.prefix) + ':rw')
        self.assertEqual(len(scan), 4)
        self.engine.command = ['/usr/bin/ludusavi']
        self.assertEqual(self.engine.access_command(paths, writable=True), ['/usr/bin/ludusavi'])

    def test_emulator_location_is_resolved_inside_prefix_and_rejects_escape(self):
        folder = self.prefix / 'drive_c/Users/Public/Documents/Steam/CODEX/42'
        folder.mkdir(parents=True)
        install = self.root / 'game'; install.mkdir()
        ini = install / 'steam_emu.ini'
        ini.write_text('[Settings]\nAppId=42\nUserName=CODEX\n')
        context = dict(self.context, directory=str(install))
        self.assertEqual(emulator_save_paths(context), [str(folder)])
        ini.write_text('[Settings]\nAppId=42\nUserName=../outside\n')
        self.assertEqual(emulator_save_paths(context), [])
        ini.write_text('[Settings]\nAppId=invalid\nUserName=CODEX\n')
        self.assertEqual(emulator_save_paths(context), [])

    def test_gse_saves_match_only_current_appid_and_cannot_escape_prefix(self):
        root = self.prefix / 'drive_c/users/steamuser/AppData/Roaming/GSE Saves'
        (root / '42').mkdir(parents=True)
        (root / '99').mkdir()
        self.assertEqual(gse_save_paths(self.context), [str(root / '42')])
        self.assertEqual(gse_save_paths(dict(self.context, steam_id='../99')), [])
        (root / '42').rmdir()
        outside = self.root / 'outside'; outside.mkdir()
        (root / '42').symlink_to(outside, target_is_directory=True)
        self.assertEqual(gse_save_paths(self.context), [])

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
    def setUp(self):
        gui=patch("ludusavi_under_test.plugin.ludusavi_gui_open",return_value=False)
        gui.start();self.addCleanup(gui.stop)

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
            window.game_detection = SimpleNamespace(status=lambda identity: 'Stopped')
            window.game_status_changed = Mock()
            queue = BackupQueue(window); tasks = []
            activity = []
            window.game_status_changed.side_effect = lambda identity, status: activity.append((identity, queue.active[0]['Id'] if queue.active else None))
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
                window.game_status_changed.assert_any_call('a', 'Stopped')
                window.game_status_changed.assert_any_call('b', 'Stopped')
                self.assertEqual(window.game_status_changed.call_args.args, ('b', 'Stopped'))
                self.assertIn(('a', None), activity)
                self.assertEqual(activity[-1], ('b', None))
            window.close()

class SeamlessQueueTests(unittest.TestCase):
    def test_repeated_sync_requests_are_debounced_and_gui_defers_them(self):
        window=QMainWindow();window.games=[]
        plugin=Plugin();plugin.sync_definitions(window);plugin.sync_definitions(window)
        self.assertTrue(plugin._sync_timer.isActive())
        self.assertEqual(plugin._sync_timer.interval(),750)
        with patch('ludusavi_under_test.plugin.ludusavi_gui_open',return_value=True):
            plugin._dispatch_sync()
        self.assertIsNotNone(plugin._sync_request)
        self.assertEqual(plugin._sync_timer.interval(),3000)
        plugin._sync_timer.stop();window.close()

    def test_backup_stays_queued_while_ludusavi_gui_is_open(self):
        window=QMainWindow();queue=BackupQueue(window)
        with patch('ludusavi_under_test.plugin.ludusavi_gui_open',return_value=True):
            queue.add({'Id':'one','Name':'Example'},{},{'state':'/tmp/playlite-test-unused'})
        self.assertIsNone(queue.active)
        self.assertEqual(len(queue.pending),1)
        queue.wait_timer.stop();window.close()

class StartupCacheTests(unittest.TestCase):
    setUp=BackupTests.setUp
    def test_persisted_fingerprint_skips_unchanged_startup(self):
        from ludusavi_under_test.definitions import definition_fingerprint
        window=QMainWindow();window.games=[{'Id':'one','Name':'Example'}]
        plugin=Plugin();plugin.options=lambda:self.engine.settings
        fingerprint=definition_fingerprint(window.games, self.engine.settings)
        self.engine.state.mkdir();(self.engine.state/'definition-sync.fingerprint').write_text(fingerprint)
        plugin.sync_definitions(window)
        with patch('ludusavi_under_test.plugin.ludusavi_gui_open',return_value=False),patch('ludusavi_under_test.plugin.QThreadPool.globalInstance',return_value=SimpleNamespace(start=lambda task: task.run())),patch.object(plugin,'_sync_definitions_now') as sync:
            plugin._dispatch_sync();sync.assert_not_called()
            window.games[0]['LudusaviBackup']={'files':['/changed/save']}
            plugin.sync_definitions(window);plugin._dispatch_sync();sync.assert_called_once()
        plugin._sync_timer.stop();window.close()

    def test_normal_ludusavi_config_rewrites_do_not_invalidate_definitions(self):
        from ludusavi_under_test.definitions import definition_fingerprint
        from unittest.mock import patch
        import os
        window = QMainWindow(); window.games = [{'Id': 'one', 'Name': 'Example'}]
        try:
            plugin = Plugin(); plugin.options = lambda: self.engine.settings
            expected = definition_fingerprint(window.games, self.engine.settings)
            self.engine.state.mkdir()
            (self.engine.state / 'definition-sync.fingerprint').write_text(expected)
            config = dict(self.original, backup={'path': '/changed/backups'}, view={'filter': 'Example'})
            (self.config / 'config.yaml').write_text(yaml.safe_dump(config, sort_keys=False))
            manifest = self.config / 'manifest.yaml'
            os.utime(manifest, ns=(manifest.stat().st_atime_ns, manifest.stat().st_mtime_ns + 1000000))
            self.assertEqual(definition_fingerprint(window.games, self.engine.settings), expected)
            with patch('ludusavi_under_test.plugin.ludusavi_gui_open', return_value=False), patch('ludusavi_under_test.plugin.QThreadPool.globalInstance', return_value=SimpleNamespace(start=lambda task: task.run())), patch.object(plugin, '_sync_definitions_now') as sync:
                plugin.sync_definitions(window); plugin._dispatch_sync()
                sync.assert_not_called()
                config['customGames'] = [{'name': 'Example', 'files': ['/new/save']}]
                (self.config / 'config.yaml').write_text(yaml.safe_dump(config))
                plugin.sync_definitions(window); plugin._dispatch_sync()
                sync.assert_called_once()
            plugin._sync_timer.stop()
        finally:
            window.close()
