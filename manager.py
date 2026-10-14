"""One game-save window for detection, definitions, history and restore."""
import copy
import json
from datetime import datetime, timezone
import fcntl
from pathlib import Path
from PyQt6.QtCore import Qt, QThreadPool, QTimer
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QLabel,
    QPushButton, QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QHeaderView, QCheckBox)
from playlite.lifecycle import run_dialog, show_warning
from playlite.metadata_dialog import Task
from playlite.play_actions import actions_for
from .backup import BackupEngine, resolve_context
from .definitions import read_definition, rule_sources
from .bulk_definitions import apply_overrides
from .restore import preview_restore, restore_backup


def backup_age(value):
    try:
        when=datetime.fromisoformat(value.replace('Z','+00:00'))
        seconds=max(0,int((datetime.now(timezone.utc)-when).total_seconds()))
    except (ValueError,TypeError):return value
    if seconds<60:return 'just now'
    if seconds<3600:return f'{seconds//60} minutes ago'
    if seconds<86400:return f'{seconds//3600} hours ago'
    return f'{seconds//86400} days ago'


def save_status(plugin, window, game):
    controller = plugin.controller(window)
    result = controller.results.get(game['Id'])
    if result is None:
        try: result = json.loads((Path(plugin.options()['state']) / 'backup-results.json').read_text()).get(game['Id'], {})
        except (OSError, ValueError): result = {}
    return result or {}


def show_manager(plugin, window, game, restore=False):
    dialog = QDialog(window); dialog.setWindowTitle('Saves — ' + game['Name']); dialog.resize(900, 720)
    layout = QVBoxLayout(dialog)
    status = QLabel(); status.setWordWrap(True); layout.addWidget(status)
    toolbar = QHBoxLayout(); layout.addLayout(toolbar)
    backup = QPushButton('Back up now'); refresh = QPushButton('Refresh'); external = QPushButton('Open Ludusavi…')
    for button in (backup, refresh, external): toolbar.addWidget(button)
    toolbar.addStretch()
    tabs = QTabWidget(); layout.addWidget(tabs)
    def page(title):
        widget = QWidget(); box = QVBoxLayout(widget); tabs.addTab(widget, title); return box
    detection = page('Detected files')
    hint = QLabel('Matching files does not prove they contain progress. Empty saves and logs may be included.'); hint.setWordWrap(True); detection.addWidget(hint)
    matches = QTreeWidget(); matches.setObjectName('matchedFiles'); matches.setHeaderLabels(['Matched file', 'Bytes']); matches.setRootIsDecorated(False)
    from playlite.desktop import open_folder
    matches.itemDoubleClicked.connect(lambda item, column: open_folder(str(Path(item.text(0)).parent)))
    matches.setTextElideMode(Qt.TextElideMode.ElideLeft); matches.header().setStretchLastSection(False)
    matches.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive); matches.setColumnWidth(0, 650)
    detection.addWidget(matches)
    confirmed = QCheckBox('I have checked that these files include my progress'); confirmed.setChecked(bool((game.get('LudusaviBackup') or {}).get('progress_confirmed'))); detection.addWidget(confirmed)
    confirm = QPushButton('Save confirmation')
    from playlite.ui_layout import action_row
    detection.addLayout(action_row(confirm))
    definition_box = page('Definition')
    origins = QTreeWidget(); origins.setHeaderLabels(['Rule', 'Source']); origins.setRootIsDecorated(False)
    origins.setMaximumHeight(160); origins.setTextElideMode(Qt.TextElideMode.ElideLeft)
    origins.header().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
    origins.header().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents)
    definition_box.addWidget(origins)
    definition_box.addWidget(QLabel('One file, folder or Ludusavi pattern per line. Saving replaces the file rules.'))
    rules = QPlainTextEdit(); definition_box.addWidget(rules)
    registry = QPlainTextEdit(); registry.setMaximumHeight(100); registry.setPlaceholderText('Registry rules, one per line'); definition_box.addWidget(registry)
    save = QPushButton('Save definition'); save.setProperty('primary',True)
    definition_box.addLayout(action_row(save))
    history_box = page('Backups and restore')
    history = QTreeWidget(); history.setHeaderLabels(['Backup date', 'ID']); history.setRootIsDecorated(False); history.header().setSectionResizeMode(0,QHeaderView.ResizeMode.ResizeToContents); history_box.addWidget(history)
    preview_button = QPushButton('Preview selected backup'); history_box.addLayout(action_row(preview_button))
    report = QPlainTextEdit(); report.setReadOnly(True); history_box.addWidget(report)
    consent = QCheckBox('Restore this backup; preserve my current files first'); history_box.addWidget(consent)
    apply = QPushButton('Restore selected backup'); apply.setProperty('primary',True); apply.setEnabled(False); history_box.addLayout(action_row(apply))
    close = QPushButton('Close'); close.clicked.connect(dialog.reject)
    footer = QHBoxLayout(); footer.addStretch(); footer.addWidget(close); layout.addLayout(footer)
    state = {'busy': False, 'preview': None, 'name': '', 'context': None}
    captured = copy.deepcopy(game); options = plugin.options(); providers = list(window.game_providers)
    results = {}; tasks = []
    def launch(work, done, kind):
        if state['busy']: return
        state['busy'] = True
        for button in (refresh, save, backup, external, preview_button, apply, confirm): button.setEnabled(False)
        identity = window.background_tasks.update(title=game['Name'], kind=kind, state='Running', summary=kind + '…') if hasattr(window, 'background_tasks') else None
        task = Task(work); tasks.append(task)
        def success(value):
            results.update(value=value, done=done)
            if identity: window.background_tasks.update(identity, state='Complete', summary=kind + ' complete.')
        def failure(error):
            results.update(error=error)
            if identity: window.background_tasks.update(identity, state='Failed', summary=kind + ' failed.', details=error)
        task.signals.succeeded.connect(success); task.signals.failed.connect(failure)
        keep = getattr(window, '_save_manager_tasks', None)
        if keep is None: keep = window._save_manager_tasks = []
        keep.append(task)
        task.signals.succeeded.connect(lambda _: keep.remove(task)); task.signals.failed.connect(lambda _: keep.remove(task))
        QThreadPool.globalInstance().start(task)
    def context():
        actions = [a for a in actions_for(captured, providers) if not a.get('IsVM') and a.get('Integration') in ('SteamIntegration', 'LutrisIntegration')]
        contexts = [resolve_context(captured, a, providers) for a in actions]
        if len({c['prefix'] for c in contexts}) > 1: raise ValueError('Multiple save prefixes: choose a prefix override in Edit before restoring.')
        if not contexts: return resolve_context(captured, None, providers)
        return contexts[0]
    def scan():
        c = context(); engine = BackupEngine(options); detected = engine.run(c, preview=True)
        d = read_definition(captured, options); name = detected.get('name') or d.get('name') or captured['Name']
        engine.read_paths = [c['prefix'], c['directory']]
        backups = engine.execute(['backups', '--api', '--', name], engine.config)
        return {'context': c, 'detected': detected, 'definition': d, 'sources': rule_sources(captured,d,options,c), 'name': name, 'history': backups.get('games', {}).get(name, {}).get('backups', [])}
    def loaded(value):
        state.update(context=value['context'], name=value['name'], preview=None)
        matches.clear(); history.clear()
        files = value['detected'].get('files', [])
        for p in files:
            try: size = str(Path(p).stat().st_size)
            except OSError: size = 'Unavailable'
            item = QTreeWidgetItem([p, size]); item.setToolTip(0,p); matches.addTopLevelItem(item)
        summary = f'{len(files)} files detected' if files else 'No saves found yet'
        state['summary'] = summary
        status.setText(summary + '\n' + status_text())
        record=Path(options['state'])/'detection-results.json'
        try: records=json.loads(record.read_text()) if record.exists() else {}
        except (OSError,ValueError): records={}
        records[game['Id']]={'summary':summary,'files':len(files)}
        record.write_text(json.dumps(records,indent=2))
        origins.clear()
        for kind, pattern, source in value.get('sources', []):
            item=QTreeWidgetItem([pattern,source]); item.setToolTip(0,kind + ': ' + pattern); origins.addTopLevelItem(item)
        d = value['definition']; rules.setPlainText('\n'.join(d.get('files', []))); registry.setPlainText('\n'.join(d.get('registry', [])))
        for row in value['history']:
            item = QTreeWidgetItem([row.get('when', ''), row['name']]); item.setData(0,Qt.ItemDataRole.UserRole,row['name']); history.addTopLevelItem(item)
        if history.topLevelItemCount(): history.setCurrentItem(history.topLevelItem(0))
        confirm.setEnabled(bool(files)); consent.setChecked(False)
    def status_text():
        r = save_status(plugin,window,game)
        cloud = r.get('details') if r.get('cloud') in ('In progress', 'Waiting') else r.get('cloud', 'Not attempted')
        timing = r.get('timings', {})
        times = ' · '.join(label + ': ' + str(timing[key]) + 's' for key, label in
                           [('scan_seconds', 'Scan'), ('local_seconds', 'Local'), ('cloud_seconds', 'Cloud')] if key in timing)
        return 'Backup: ' + r.get('status','No backup recorded') + (' · ' + backup_age(r['updated']) if r.get('updated') else '') + '\nCloud: ' + str(cloud) + ('\n' + times if times else '')
    def persist(patch):
        try:
            window.games = apply_overrides(window.data, {game['Id']:patch}); captured.setdefault('LudusaviBackup',{}).update(patch)
            window.refresh_library()
        except (OSError,ValueError) as error:
            show_warning(dialog,'Save definition',str(error));return False
        return True
    confirm.clicked.connect(lambda: persist({'progress_confirmed':confirmed.isChecked()}))
    def save_rules():
        if not persist({'files':[p.strip() for p in rules.toPlainText().splitlines() if p.strip()], 'registry':[p.strip() for p in registry.toPlainText().splitlines() if p.strip()], 'progress_confirmed':False}): return
        confirmed.setChecked(False)
        plugin.sync_definitions(window); status.setText('Definition update queued…');reload_timer.start()
    reload_timer=QTimer(dialog);reload_timer.setInterval(1000)
    def reload_when_synced():
        if not getattr(plugin,'_sync_busy',False) and not getattr(plugin,'_sync_request',None):
            reload_timer.stop();launch(scan,loaded,'Save inspection')
    reload_timer.timeout.connect(reload_when_synced)
    save.clicked.connect(save_rules)
    backup.clicked.connect(lambda: plugin.backup(window, captured)); external.clicked.connect(lambda: plugin.open_ludusavi(window,captured))
    refresh.clicked.connect(lambda: launch(scan,loaded,'Save inspection'))
    def preview_selected():
        selected=history.currentItem()
        if not selected or not state['context']:return
        chosen=selected.data(0,Qt.ItemDataRole.UserRole)
        def done(value):
            state['preview']=(chosen,value)
            lines=[p + ' — ' + info.get('change','') for row in value.get('games',{}).values() for p,info in row.get('files',{}).items()]
            report.setPlainText('\n'.join(lines) or 'No files in this backup.'); consent.setChecked(False)
        launch(lambda: preview_restore(options,state['context'],state['name'],chosen),done,'Restore preview')
    preview_button.clicked.connect(preview_selected)
    def invalidate(): state['preview']=None; apply.setEnabled(False); consent.setChecked(False); report.clear()
    history.currentItemChanged.connect(lambda *_:invalidate())
    consent.toggled.connect(lambda checked: apply.setEnabled(checked and bool(state['preview']) and not state['busy']))
    def do_restore():
        controller=plugin.controller(window)
        if controller.active or controller.pending: show_warning(dialog,'Restore','Wait for queued backups to finish.');return
        if window.game_detection.status(game['Id']) in ('Running','Launching'):show_warning(dialog,'Restore','Close the game before restoring.');return
        chosen,expected=state['preview'];c=copy.deepcopy(state['context']);name=state['name']
        def work():
            state_path=Path(options['state']);state_path.mkdir(parents=True,exist_ok=True)
            with (state_path/'backup.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                for provider in providers:
                    if callable(getattr(provider,'detect_running',None)) and provider.owns(captured) and game['Id'] in provider.detect_running([captured]):raise ValueError('Close the game before restoring.')
                return restore_backup(options,c,name,chosen,expected)
        def done(value):
            status.setText('Saves restored.');report.setPlainText('Previous files preserved in:\n'+value['recovery']);state['preview']=None;consent.setChecked(False)
        launch(work,done,'Save restore')
    apply.clicked.connect(do_restore)
    timer=QTimer(dialog);timer.setInterval(100)
    def poll():
        text = status_text()
        if text != state.get('backup_status'):
            state['backup_status'] = text
            if not state['busy'] and state.get('summary'):
                status.setText(state['summary'] + '\n' + text)
        if not results:return
        value=dict(results);results.clear();state['busy']=False
        for button in (refresh,save,backup,external,preview_button,confirm):button.setEnabled(True)
        if 'error' in value:
            status.setText('Needs attention: '+value['error'])
            state['preview']=None;apply.setEnabled(False)
        else:value['done'](value['value'])
    timer.timeout.connect(poll);timer.start()
    if restore:tabs.setCurrentIndex(2)
    launch(scan,loaded,'Save inspection')
    run_dialog(dialog)
