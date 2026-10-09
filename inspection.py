"""Show configured rules and a read-only scan without claiming proven progress."""
import copy
import json
import yaml
from pathlib import Path
from PyQt6.QtCore import QThreadPool, QTimer
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
    QPlainTextEdit, QPushButton, QVBoxLayout)
from playlite.lifecycle import run_dialog, show_warning
from playlite.metadata_dialog import Task
from playlite.play_actions import actions_for
from .backup import BackupEngine, resolve_context
from .definitions import read_definition


def show_inspection(plugin, window, game):
    dialog = QDialog(window); dialog.setWindowTitle('Save detection — ' + game['Name']); dialog.resize(850, 600)
    layout = QVBoxLayout(dialog)
    summary = QLabel('Scanning configured save locations…'); summary.setWordWrap(True); layout.addWidget(summary)
    options = plugin.options()
    try:
        definition = read_definition(game, options)
    except (OSError, ValueError, yaml.YAMLError) as error:
        show_warning(window, 'Save detection', str(error))
        return
    saved = game.get('LudusaviBackup') or {}
    paths = saved.get('files', definition.get('files', []))
    registry = saved.get('registry', definition.get('registry', []))
    rules = QPlainTextEdit('File rules:\n' + ('\n'.join(paths) if paths else 'No custom file rules. Sync a definition to inspect the manifest rules.') + '\n\nRegistry rules:\n' + '\n'.join(registry))
    rules.setReadOnly(True); rules.setMaximumHeight(170); layout.addWidget(rules)
    files = QPlainTextEdit(); files.setReadOnly(True); layout.addWidget(files)
    last = QLabel(); last.setWordWrap(True); layout.addWidget(last)
    record = Path(options['state']) / 'backup-results.json'
    try:
        previous = json.loads(record.read_text()).get(game['Id'], {})
    except (OSError, ValueError): previous = {}
    last.setText('Last backup: ' + previous.get('status', 'None recorded') + (' · ' + previous['updated'] if previous.get('updated') else '') + '\nCloud: ' + previous.get('cloud', 'Not attempted'))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    copy_button = buttons.addButton('Copy report', QDialogButtonBox.ButtonRole.ActionRole)
    edit = buttons.addButton('Edit definition…', QDialogButtonBox.ButtonRole.ActionRole)
    layout.addWidget(buttons); buttons.rejected.connect(dialog.reject)
    copy_button.clicked.connect(lambda: QApplication.clipboard().setText(summary.text() + '\n\n' + rules.toPlainText() + '\n\n' + files.toPlainText() + '\n\n' + last.text()))
    edit.clicked.connect(lambda: (dialog.accept(), plugin.edit_definition(window, game)))
    captured = copy.deepcopy(game); providers = list(window.game_providers)
    def work():
        results = []; seen = set()
        for action in actions_for(captured, providers):
            if action.get('IsVM') or action.get('Integration') not in ('SteamIntegration', 'LutrisIntegration'): continue
            context = resolve_context(captured, action, providers)
            identity = (context['prefix'], context['directory'])
            if identity in seen: continue
            seen.add(identity)
            result = BackupEngine(options).run(context, preview=True)
            results += result.get('files', [])
        return list(dict.fromkeys(results))
    result = {}
    background = getattr(window, 'background_tasks', None)
    identity = background.update(title=game['Name'], kind='Save inspection', state='Running', summary='Scanning configured save locations…') if background is not None else None
    task = Task(work)
    if background is not None:
        task.signals.succeeded.connect(lambda found: background.update(identity, state='Complete', summary=f'{len(found)} files detected; progress contents require confirmation.', details='\n'.join(found)))
        task.signals.failed.connect(lambda error: background.update(identity, state='Failed', summary='Save detection failed.', details=error))
    task.signals.succeeded.connect(lambda found: result.update(files=found))
    task.signals.failed.connect(lambda error: result.update(error=error))
    # Poll a Python result rather than invoking widgets from a worker after closure.
    timer = QTimer(dialog); timer.setInterval(100)
    def refresh():
        if not result: return
        timer.stop()
        if 'error' in result:
            summary.setText('Save detection failed.'); files.setPlainText(result['error']); return
        found = result['files']
        confidence = 'Progress confirmed by you.' if saved.get('progress_confirmed') else 'Progress contents have not been confirmed.'
        summary.setText(f'{len(found)} files detected. {confidence} No save files were changed.')
        files.setPlainText('\n'.join(found) or 'No files found. This game may not have save data yet, or its rules may need correction.')
    timer.timeout.connect(refresh); timer.start()
    tasks = getattr(window, '_ludusavi_inspection_tasks', None)
    if tasks is None: tasks = window._ludusavi_inspection_tasks = []
    tasks.append(task)
    task.signals.succeeded.connect(lambda _: tasks.remove(task)); task.signals.failed.connect(lambda _: tasks.remove(task))
    QThreadPool.globalInstance().start(task)
    run_dialog(dialog)
