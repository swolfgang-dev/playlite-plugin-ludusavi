import copy
import json
from datetime import datetime, timezone
import os
import yaml
from pathlib import Path
import subprocess
from PyQt6.QtCore import QObject, QSettings, QThreadPool, QTimer, pyqtSlot, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QWidget)
from playlite.providers import GenericPlugin
from playlite.lifecycle import run_dialog, show_warning
from playlite.metadata_dialog import Task
from .backup import BackupEngine, host_environment, resolve_context, ludusavi_gui_open


class BackupQueue(QObject):
    progress = pyqtSignal(str)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.pending = []
        self.active = None
        self.results = {}
        self.task_ids = {}
        self.states = {}
        self._task = None
        self.wait_timer=QTimer(self);self.wait_timer.setSingleShot(True);self.wait_timer.timeout.connect(self.start_next)
        self.upload_retries = {}
        self.progress.connect(self.update_progress)

    def refresh_launch_activity(self, identity):
        refresh = getattr(self.window, 'game_status_changed', None)
        detection = getattr(self.window, 'game_detection', None)
        if callable(refresh) and detection is not None:
            refresh(identity, detection.status(identity))

    def add(self, game, action, settings):
        identity = game['Id']
        if self.active and self.active[0]['Id'] == identity or any(job[0]['Id'] == identity for job in self.pending):
            self.window.statusBar().showMessage('Save backup already queued for ' + game['Name'], 8000)
            return
        self.pending.append((copy.deepcopy(game), copy.deepcopy(action), settings))
        self.states[identity] = Path(settings['state'])
        registry = getattr(self.window, 'background_tasks', None)
        if registry is not None:
            self.task_ids[identity] = registry.update(title=game['Name'], kind='Save backup', state='Queued',
                summary='Waiting for the current backup to finish.', retry=lambda: self.add(game, action, settings))
        self.results[identity] = {'status': 'Backup queued', 'cloud': 'Waiting', 'details': 'Waiting for the current backup to finish.'}
        if self.active:
            self.window.statusBar().showMessage('Save backup queued for ' + game['Name'], 8000)
        self.start_next()
        self.refresh_launch_activity(identity)

    def start_next(self):
        if self.active or not self.pending:
            return
        if ludusavi_gui_open():
            self.wait_timer.start(3000)
            return
        self.active = self.pending.pop(0)
        game, action, settings = self.active
        providers = list(self.window.game_providers)
        self.window.statusBar().showMessage('Backing up saves: ' + game['Name'])
        self.update_progress('Backing up saves…')
        def work():
            import fcntl
            state = Path(settings['state']); state.mkdir(parents=True, exist_ok=True)
            with (state / 'backup.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                for provider in providers:
                    if callable(getattr(provider, 'detect_running', None)) and provider.owns(game) and game['Id'] in provider.detect_running([game]):
                        raise ValueError('Close the game before backing up its saves.')
                from .definitions import sync_definitions
                from playlite.library_storage import library_lock
                with library_lock(self.window.data):
                    latest = json.loads((self.window.data / 'library.json').read_text())
                    current = next((g for g in latest if g['Id'] == game['Id']), None)
                    if current is None:
                        raise ValueError('This game was removed before its queued backup started.')
                    sync_definitions([current], providers, settings)
                context = resolve_context(game, action, providers)
                return BackupEngine(settings).run(context, status=self.progress.emit)
        self._task = Task(work)
        self._task.signals.succeeded.connect(self.completed)
        self._task.signals.failed.connect(self.failed)
        QThreadPool.globalInstance().start(self._task)

    def retry_upload(self, captured):
        game, action, settings = captured
        current = next((g for g in self.window.games if g['Id'] == game['Id']), None)
        if current is None or self.window.game_detection.status(game['Id']) in ('Running','Launching'): return
        mode=(current.get('LudusaviBackup') or {}).get('automatic','default')
        if mode=='disabled' or not settings.get('cloud'): return
        self.add(current, action, settings)

    @pyqtSlot(str)
    def update_progress(self, message):
        if not self.active:
            return
        game = self.active[0]
        self.results[game['Id']] = {'status': message, 'cloud': 'Waiting' if message.startswith('Waiting') else 'In progress',
                                    'details': message}
        self.window.statusBar().showMessage(game['Name'] + ': ' + message)
        registry = getattr(self.window, 'background_tasks', None)
        if registry is not None:
            registry.update(self.task_ids[game['Id']], state='Waiting' if message.startswith('Waiting') else 'Uploading' if message.startswith('Uploading') else 'Running', summary=message)
        self.refresh_launch_activity(game['Id'])

    @pyqtSlot(object)
    def completed(self, result):
        game = self.active[0]
        result = dict(result, updated=datetime.now(timezone.utc).isoformat(timespec='seconds'))
        self.results[game['Id']] = result
        state = self.states.get(game['Id'])
        if state is not None:
            state.mkdir(parents=True, exist_ok=True)
            path = state / 'backup-results.json'
            try:
                history = json.loads(path.read_text()) if path.exists() else {}
                history[game['Id']] = result
                temp = path.with_suffix('.tmp'); temp.write_text(json.dumps(history, indent=2)); temp.chmod(0o600); temp.replace(path)
            except (OSError, ValueError):
                import logging
                logging.exception('Could not record backup result')
        registry = getattr(self.window, 'background_tasks', None)
        if registry is not None:
            registry.update(self.task_ids[game['Id']], state='Failed' if result['status'] == 'Backup failed' else 'Needs attention' if result.get('cloud') in ('Needs attention', 'Upload failed') else 'Complete',
                summary=result['status'] + ' · Cloud: ' + result.get('cloud', 'Not attempted'), details=result.get('details', ''))
        if result.get('cloud') == 'Upload failed':
            attempt = self.upload_retries.get(game['Id'], 0)
            if attempt < 3:
                self.upload_retries[game['Id']] = attempt + 1
                captured = copy.deepcopy(self.active)
                retry_timer = QTimer(self); retry_timer.setSingleShot(True)
                retry_timer.timeout.connect(lambda captured=captured, timer=retry_timer: (self.retry_upload(captured), timer.deleteLater()))
                retry_timer.start(60000 * (attempt + 1))
        elif result.get('cloud') == 'Uploaded': self.upload_retries.pop(game['Id'], None)
        message = f"{game['Name']}: {result['status']}"
        if result.get('cloud') not in ('Uploaded', 'Disabled', 'Not attempted'):
            message += ' — cloud needs attention (see Save backup status)'
        self.window.statusBar().showMessage(message, 15000)
        self.active = None
        self._task = None
        self.refresh_launch_activity(game['Id'])
        self.start_next()

    @pyqtSlot(str)
    def failed(self, error):
        self.completed({'status': 'Backup failed', 'local_saved': False, 'cloud': 'Not attempted', 'details': error})


class Plugin(GenericPlugin):
    def __init__(self):
        self.sessions = {}

    def settings(self):
        return QSettings('Playlite', 'Ludusavi')

    def options(self):
        settings = self.settings()
        env = host_environment()
        state = Path(env.get('XDG_STATE_HOME', str(Path(env['HOME']) / '.local/state'))) / 'playlite/ludusavi'
        return {'persistent_access': True, 'automatic': settings.value('automatic', True, type=bool),
                'cloud': settings.value('cloud', True, type=bool),
                'native': settings.value('native', '', type=str),
                'config': settings.value('config', '', type=str), 'state': str(state)}

    def controller(self, window):
        if not hasattr(window, '_ludusavi_backups'):
            window._ludusavi_backups = BackupQueue(window)
        return window._ludusavi_backups

    def after_launch(self, window, game, action):
        self.sessions[game['Id']] = copy.deepcopy((game, action))

    def after_game_stopped(self, window, game, action):
        captured = self.sessions.pop(game['Id'], None)
        # Untracked/external launches cannot identify the prefix that was used.
        if not captured or not action:
            return
        launch_game, launch_action = captured
        launch_game['LudusaviBackup'] = copy.deepcopy(game.get('LudusaviBackup') or {})
        mode = launch_game['LudusaviBackup'].get('automatic', 'default')
        options = self.options()
        if mode == 'disabled' or mode == 'default' and not options['automatic']:
            return
        if launch_action.get('Integration') not in ('SteamIntegration', 'LutrisIntegration') or launch_action.get('IsVM'):
            return
        self.controller(window).add(launch_game, launch_action, options)

    action_group = 'Saves'

    def game_actions(self, window, game):
        from .manager import show_manager
        return [('Back up now', lambda: self.backup(window, game)),
                ('Restore…', lambda: show_manager(self, window, game, restore=True)),
                ('Manage saves…', lambda: show_manager(self, window, game))]

    def launch_activity(self, window, game):
        controller=self.controller(window)
        if controller.active and controller.active[0]['Id']==game['Id']:
            result=controller.results.get(game['Id'],{})
            return 'Uploading saves' if result.get('cloud')=='In progress' and result.get('status','').startswith('Uploading') else 'Backing up saves'
        if any(g['Id']==game['Id'] for g,_,_ in controller.pending): return 'Save backup queued'
        return ''

    def readiness_checks(self, window, game):
        from playlite.play_actions import actions_for
        actions = [a for a in actions_for(game, window.game_providers) if a.get('Integration') in ('SteamIntegration','LutrisIntegration') and not a.get('IsVM')]
        if not actions: return {'issues': [], 'checked': ['Save backup is not configured for this launch type.']}
        issues=[]; checked=[]
        for action in actions:
            try:
                resolve_context(game,action,window.game_providers)
                from .definitions import read_definition
                definition=read_definition(game,self.options())
                if definition: checked.append('Save definition and host prefix are available.')
                else: issues.append('Save definition needs synchronization; open Manage saves.')
            except (OSError,ValueError) as error: issues.append('Save backup: '+str(error))
        return {'issues':list(dict.fromkeys(issues)), 'checked':list(dict.fromkeys(checked))}

    def batch_game_actions(self, window, games):
        from .bulk_definitions import edit_definitions
        return [('Sync save definitions from manifest…', lambda: self.sync_definitions(window, games)),
                ('Edit selected save definitions…', lambda: edit_definitions(self, window, games))]

    def sync_definitions(self, parent, games=None, settings=None):
        window = parent
        while window is not None and not hasattr(window, 'games'):
            window = window.parent()
        if window is None: return
        self._sync_request = (window, settings)
        if not hasattr(self, '_sync_timer'):
            self._sync_timer = QTimer(window); self._sync_timer.setSingleShot(True)
            self._sync_timer.timeout.connect(self._dispatch_sync)
        self._sync_timer.start(750)

    def _dispatch_sync(self):
        request = getattr(self, '_sync_request', None)
        if not request: return
        if getattr(self, '_sync_busy', False):
            self._sync_timer.start(1000); return
        if ludusavi_gui_open():
            self._sync_timer.start(3000); return
        if getattr(self, '_sync_check_task', None) is not None:
            self._sync_timer.start(1000); return
        window, options = request
        options = options or self.options()
        games = copy.deepcopy(window.games)
        from .definitions import definition_fingerprint
        task = Task(lambda: definition_fingerprint(games, options))
        self._sync_check_task = task
        def checked(fingerprint):
            self._sync_check_task = None
            if self._sync_request is not request:
                self._sync_timer.start(750); return
            engine = BackupEngine(options)
            cached = getattr(self, '_sync_fingerprint', None)
            if cached is None:
                try: cached = (engine.state / 'definition-sync.fingerprint').read_text().strip()
                except OSError: pass
            self._sync_request = None
            if fingerprint and fingerprint == cached:
                return
            self._sync_busy = True
            self._sync_definitions_now(window, settings=options)
        task.signals.succeeded.connect(checked)
        task.signals.failed.connect(lambda _: checked(None))
        QThreadPool.globalInstance().start(task)

    def _sync_definitions_now(self, parent, games=None, settings=None):
        window = parent
        while window is not None and not hasattr(window, 'games'):
            window = window.parent()
        if window is None:
            show_warning(parent, 'Save definitions', 'Open these settings from the game library.')
            return
        options = settings or self.options()
        games = copy.deepcopy(window.games if games is None else games)
        providers = list(window.game_providers)
        completed_fingerprint = {}
        def work():
            import fcntl
            from .definitions import sync_definitions, definition_fingerprint
            state = Path(options['state']); state.mkdir(parents=True, exist_ok=True)
            with (state / 'backup.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                # The GUI keeps its own configuration in memory and may later
                # overwrite external edits. Leave it intact and ask for closure.
                for path in Path('/proc').iterdir():
                    try:
                        if path.name.isdecimal() and path.stat().st_uid == os.getuid() and (path / 'comm').read_text().strip().lower() == 'ludusavi':
                            args = (path / 'cmdline').read_bytes().split(b'\0')
                            if b'--api' not in args:
                                raise ValueError('Close the Ludusavi window before syncing save definitions, then retry.')
                    except OSError:
                        continue
                from playlite.library_storage import library_lock
                with library_lock(window.data):
                    current = json.loads((window.data / 'library.json').read_text())
                    result = sync_definitions(current, providers, options, reconcile=True)
                    completed_fingerprint['value'] = definition_fingerprint(current, options)
                    return result
        registry = getattr(window, 'background_tasks', None)
        identity = registry.update(title='Sync save definitions', kind='Save definitions', state='Running',
            summary=f'Scanning {len(games)} games…', retry=lambda: self.sync_definitions(window, games, options)) if registry is not None else None
        task = Task(work)
        if registry is not None:
            task.signals.succeeded.connect(lambda result: registry.update(identity, state='Complete', summary=f"Updated {len(result['updated'])} games; skipped {len(result['skipped'])}.", details=json.dumps(result, indent=2)))
            task.signals.failed.connect(lambda error: registry.update(identity, state='Failed', summary='Save definition sync failed.', details=error))
        task.signals.succeeded.connect(lambda result: window.statusBar().showMessage(
            f"Save definitions synced: {len(result['updated'])}; skipped: {len(result['skipped'])}", 15000))
        task.signals.failed.connect(lambda error: window.statusBar().showMessage('Save definition update deferred: ' + error, 10000))
        # Retain tasks until their queued Qt callbacks finish.
        tasks = getattr(window, '_ludusavi_definition_tasks', None)
        if tasks is None:
            tasks = window._ludusavi_definition_tasks = []
        tasks.append(task)
        def finish_sync(*_):
            self._sync_busy = False
            if self._sync_request: self._sync_timer.start(750)
        def remember_sync(_):
            if self._sync_request:return
            self._sync_fingerprint = completed_fingerprint.get('value')
            if not self._sync_fingerprint: return
            engine = BackupEngine(options)
            engine.state.mkdir(parents=True,exist_ok=True)
            (engine.state/'definition-sync.fingerprint').write_text(self._sync_fingerprint)
        task.signals.succeeded.connect(remember_sync)
        task.signals.succeeded.connect(finish_sync); task.signals.failed.connect(finish_sync)
        task.signals.succeeded.connect(lambda result: tasks.remove(task))
        task.signals.failed.connect(lambda error: tasks.remove(task))
        window.statusBar().showMessage('Syncing Ludusavi custom save definitions…')
        QThreadPool.globalInstance().start(task)

    def library_ready(self, window):
        engine = BackupEngine(self.options())
        if (engine.config / 'config.yaml').is_file() and (engine.config / 'manifest.yaml').is_file():
            self.sync_definitions(window)

    def after_game_updated(self, window, game):
        record=Path(self.options()['state'])/'detection-results.json'
        try:
            data=json.loads(record.read_text());data.pop(game['Id'],None);record.write_text(json.dumps(data,indent=2))
        except (OSError,ValueError):pass
        self.sync_definitions(window)

    def after_game_added(self, window, game, editor):
        self.sync_definitions(window)

    def after_game_removed(self, window, game):
        self.sync_definitions(window)

    def edit_definition(self, window, game):
        from playlite.editor import MetadataEditor
        editor = MetadataEditor(game, window.data, window)
        self.augment_editor(editor) if not hasattr(editor, 'ludusavi_files') else None
        # Use the game's regular editor so save, validation and cancellation
        # have the same behavior from either entry point.
        for index in range(editor.tabs.count()):
            if editor.tabs.tabText(index) == 'Installation':
                editor.tabs.setCurrentIndex(index)
                break
        if run_dialog(editor) != QDialog.DialogCode.Accepted:
            return
        from playlite.editor import save_game
        try:
            save = lambda: save_game(window.data, window.games, editor.result_game)
            window.games = editor.play_actions.save_with_removals(save) if hasattr(editor, "play_actions") else save()
            for cache in editor.download_caches:
                cache.cleanup()
            window.refresh_library()
            self.after_game_updated(window, editor.result_game)
        except (ValueError, OSError) as error:
            show_warning(window, 'Save definition', str(error))

    def backup(self, window, game):
        try:
            game = next((entry for entry in window.games if entry['Id'] == game['Id']), game)
            if window.game_detection.status(game['Id']) in ('Running', 'Launching'):
                raise ValueError('Close the game before backing up its saves.')
            from playlite.play_actions import actions_for
            actions = [a for a in actions_for(game, window.game_providers)
                       if a.get('Integration') in ('SteamIntegration', 'LutrisIntegration') and not a.get('IsVM')]
            action = actions[0] if len(actions) == 1 else None
            if len(actions) > 1:
                from playlite.add_game import AddGameMethods
                picker = AddGameMethods([(a.get('Name') or a['Integration'], a) for a in actions], window)
                picker.setWindowTitle('Choose save prefix')
                picker.findChild(QLabel).setText('Choose the play action whose saves you want to back up.')
                if run_dialog(picker) != QDialog.DialogCode.Accepted:
                    return
                action = picker.selected_method
            if not action and not (game.get('LudusaviBackup') or {}).get('prefix'):
                raise ValueError('Add a host Steam/Lutris play action or set a host prefix in Edit → Installation.')
            self.controller(window).add(game, action, self.options())
        except (ValueError, OSError) as error:
            show_warning(window, 'Save backup', str(error))

    def open_ludusavi(self, window, game, custom=False):
        try:
            controller = self.controller(window)
            if controller.active or controller.pending:
                raise ValueError('Wait for queued save backups to finish before opening restore/configuration.')
            if window.game_detection.status(game['Id']) in ('Running', 'Launching'):
                raise ValueError('Close the game before opening restore/configuration.')
            options = self.options()
            engine = BackupEngine(options)
            from playlite.play_actions import actions_for
            contexts = [resolve_context(game, action, window.game_providers)
                        for action in actions_for(game, window.game_providers)
                        if action.get('Integration') in ('SteamIntegration', 'LutrisIntegration') and not action.get('IsVM')]
            if not contexts and (game.get('LudusaviBackup') or {}).get('prefix'):
                contexts = [resolve_context(game, None, window.game_providers)]
            from .definitions import register_detected_locations
            title = register_detected_locations(game, contexts, options)
            paths = [path for context in contexts for path in (context['prefix'], context['directory']) if path]
            configuration = yaml.safe_load((engine.config / 'config.yaml').read_text()) or {}
            paths += [p for entry in configuration.get('customGames', []) for p in entry.get('winePrefix', [])]
            command = engine.access_command(paths, writable=True)
            if options['config']:
                command += ['--config', options['config']]
            command += ['gui']
            if custom:
                command += ['--custom-game', title]
            subprocess.Popen(command, env=engine.env, start_new_session=True)
            window.statusBar().showMessage('In Ludusavi, choose Restore Mode and review the save paths before restoring.', 15000)
        except (OSError, ValueError) as error:
            show_warning(window, 'Ludusavi', str(error))

    def create_settings(self, parent=None):
        widget = QWidget(parent); form = QFormLayout(widget); options = self.options()
        widget.automatic = QCheckBox('Automatically back up host games when they exit'); widget.automatic.setChecked(options['automatic']); form.addRow(widget.automatic)
        widget.cloud = QCheckBox('Use Ludusavi’s configured automatic cloud synchronization'); widget.cloud.setChecked(options['cloud']); form.addRow(widget.cloud)
        widget.native = QLineEdit(options['native']); widget.native.setPlaceholderText('Leave empty to use the Ludusavi Flatpak'); form.addRow('Native executable', widget.native)
        widget.config = QLineEdit(options['config']); widget.config.setPlaceholderText('Use Ludusavi’s standard configuration'); form.addRow('Configuration folder', widget.config)
        sync = QPushButton('Sync library save definitions from manifest…')
        sync.clicked.connect(lambda: self.sync_definitions(widget, settings=dict(self.options(),
            native=widget.native.text().strip(), config=widget.config.text().strip())))
        form.addRow(sync)
        hint = QLabel('Local backups use Ludusavi’s destination and retention. Cloud conflicts leave the cloud untouched. Automatic backup requires a game launched from Playlite using a host Steam/Lutris action. Configure additional save locations with Ludusavi Custom Games.'); hint.setWordWrap(True); form.addRow(hint)
        return widget

    def save_settings(self, widget):
        for field in (widget.native, widget.config):
            if field.text().strip() and not Path(field.text().strip()).is_absolute():
                raise ValueError('Ludusavi executable/configuration paths must be absolute.')
        settings = self.settings()
        for key, value in {'automatic': widget.automatic.isChecked(), 'cloud': widget.cloud.isChecked(), 'native': widget.native.text().strip(), 'config': widget.config.text().strip()}.items():
            settings.setValue(key, value)
        settings.sync()

    def augment_editor(self, editor):
        if getattr(editor, 'editor_purpose', '') == 'play_action':
            return
        saved = editor.game.get('LudusaviBackup') or {}
        from .definitions import read_definition
        try:
            definition = read_definition(editor.game, self.options())
        except (OSError, ValueError, yaml.YAMLError):
            definition = {}
        editor.ludusavi_mode = QComboBox()
        for label, value in [('Use global setting', 'default'), ('Enabled', 'enabled'), ('Disabled', 'disabled')]:
            editor.ludusavi_mode.addItem(label, value)
        editor.ludusavi_mode.setCurrentIndex(max(0, editor.ludusavi_mode.findData(saved.get('automatic', 'default'))))
        editor.ludusavi_name = QLineEdit(saved.get('name') or definition.get('name', '')); editor.ludusavi_name.setPlaceholderText('Match automatically using Steam ID or game title')
        editor.ludusavi_prefix = QLineEdit(saved.get('prefix', '')); editor.ludusavi_prefix.setPlaceholderText('Use the host prefix from the chosen play action')
        editor.installation_form.addRow('Automatic save backup', editor.ludusavi_mode)
        editor.installation_form.addRow('Ludusavi game title', editor.ludusavi_name)
        editor.installation_form.addRow('Save prefix override', editor.ludusavi_prefix)
        editor.ludusavi_files = QPlainTextEdit('\n'.join(saved.get('files', definition.get('files', []))))
        editor.ludusavi_files.setMaximumHeight(120)
        editor.ludusavi_files.setPlaceholderText('One save file, folder, or Ludusavi pattern per line')
        editor.ludusavi_registry = QPlainTextEdit('\n'.join(saved.get('registry', definition.get('registry', []))))
        editor.ludusavi_registry.setMaximumHeight(80)
        editor.installation_form.addRow('Save definition paths', editor.ludusavi_files)
        editor.ludusavi_confirmed = QCheckBox('I have confirmed these files contain my game progress')
        editor.ludusavi_confirmed.setChecked(saved.get('progress_confirmed', False))
        editor.installation_form.addRow(editor.ludusavi_confirmed)
        editor.installation_form.addRow('Save registry keys', editor.ludusavi_registry)
        for field in (editor.ludusavi_files, editor.ludusavi_registry):
            field.textChanged.connect(lambda: editor.ludusavi_confirmed.setChecked(False))
        editor.ludusavi_name.textChanged.connect(lambda: editor.ludusavi_confirmed.setChecked(False))
        editor.ludusavi_prefix.textChanged.connect(lambda: editor.ludusavi_confirmed.setChecked(False))
        editor.ludusavi_has_definition = bool(definition or 'files' in saved)

    def collect_editor(self, editor, game):
        if not hasattr(editor, 'ludusavi_mode'):
            return
        prefix = editor.ludusavi_prefix.text().strip()
        if prefix and (not Path(prefix).is_absolute() or not (Path(prefix) / 'drive_c').is_dir()):
            raise ValueError('The save prefix override must be an existing absolute Wine/Proton prefix containing drive_c.')
        game['LudusaviBackup'] = {'automatic': editor.ludusavi_mode.currentData(), 'name': editor.ludusavi_name.text().strip(), 'prefix': prefix, 'progress_confirmed': editor.ludusavi_confirmed.isChecked()}
        if editor.ludusavi_has_definition or editor.ludusavi_files.toPlainText().strip() or editor.ludusavi_registry.toPlainText().strip():
            game['LudusaviBackup'].update(files=[p.strip() for p in editor.ludusavi_files.toPlainText().splitlines() if p.strip()],
                registry=[p.strip() for p in editor.ludusavi_registry.toPlainText().splitlines() if p.strip()])
