"""Preview and atomically edit only the selected games' save-backup settings."""
import copy
import json
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QPlainTextEdit, QVBoxLayout)
from playlite.lifecycle import run_dialog, show_warning
from playlite.library_storage import library_lock
from .definitions import read_definition


def apply_overrides(data, updates):
    with library_lock(data):
        path = data / 'library.json'
        games = json.loads(path.read_text())
        known = {game['Id'] for game in games}
        if not set(updates) <= known:
            raise ValueError('A selected game was removed. Refresh the library and retry.')
        for game in games:
            if game['Id'] in updates:
                game.setdefault('LudusaviBackup', {}).update(copy.deepcopy(updates[game['Id']]))
        temporary = path.with_name('library.bulk-saves.tmp')
        try:
            temporary.write_text(json.dumps(games, indent=2))
            temporary.chmod(path.stat().st_mode & 0o777)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return games


def edit_definitions(plugin, window, games):
    dialog = QDialog(window); dialog.setWindowTitle('Edit selected save definitions'); dialog.resize(750, 620)
    layout = QVBoxLayout(dialog)
    hint = QLabel(f'Edit {len(games)} games. Each game keeps its own title and prefix. Only checked fields change.'); hint.setWordWrap(True); layout.addWidget(hint)
    form = QFormLayout(); layout.addLayout(form)
    automatic = QComboBox()
    for title, value in [('Keep current setting', None), ('Use global setting', 'default'), ('Enable', 'enabled'), ('Disable', 'disabled')]: automatic.addItem(title, value)
    form.addRow('Automatic backups', automatic)
    replace_files = QCheckBox('Replace save file rules'); layout.addWidget(replace_files)
    paths = QPlainTextEdit(); paths.setPlaceholderText('One file, folder, or Ludusavi pattern per line. These replace the current rules.'); paths.setMaximumHeight(120); paths.setEnabled(False); replace_files.toggled.connect(paths.setEnabled); layout.addWidget(paths)
    replace_registry = QCheckBox('Replace registry rules'); layout.addWidget(replace_registry)
    registry = QPlainTextEdit(); registry.setMaximumHeight(80); registry.setEnabled(False); replace_registry.toggled.connect(registry.setEnabled); layout.addWidget(registry)
    preview = QPlainTextEdit(); preview.setReadOnly(True); layout.addWidget(preview)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel); layout.addWidget(buttons)
    buttons.rejected.connect(dialog.reject); buttons.accepted.connect(dialog.accept)
    def changes():
        result = {}
        if automatic.currentData() is not None: result['automatic'] = automatic.currentData()
        if replace_files.isChecked(): result.update(files=[p.strip() for p in paths.toPlainText().splitlines() if p.strip()], progress_confirmed=False)
        if replace_registry.isChecked(): result.update(registry=[p.strip() for p in registry.toPlainText().splitlines() if p.strip()], progress_confirmed=False)
        return result
    def refresh(*_):
        patch = changes(); buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(patch))
        preview.setPlainText('\n\n'.join(game['Name'] + '\n' + '\n'.join(key + ': ' + json.dumps((game.get('LudusaviBackup') or {}).get(key, 'Use manifest/global setting')) + ' → ' + json.dumps(value) for key, value in patch.items()) for game in games))
    automatic.currentIndexChanged.connect(refresh); replace_files.toggled.connect(refresh); replace_registry.toggled.connect(refresh)
    paths.textChanged.connect(refresh); registry.textChanged.connect(refresh); refresh()
    if run_dialog(dialog) != QDialog.DialogCode.Accepted: return
    try:
        patch = changes()
        if not patch: return
        updates = {}
        for game in games:
            update = copy.deepcopy(patch)
            if 'files' in patch or 'registry' in patch:
                definition = read_definition(game, plugin.options())
                previous = game.get('LudusaviBackup') or {}
                update['name'] = previous.get('name') or definition.get('name') or game['Name']
                if 'files' not in patch: update['files'] = previous.get('files', definition.get('files', []))
                if 'registry' not in patch: update['registry'] = previous.get('registry', definition.get('registry', []))
            updates[game['Id']] = update
        window.games = apply_overrides(window.data, updates)
        window.refresh_library()
        selected = [game for game in window.games if game['Id'] in {g['Id'] for g in games}]
        if 'files' in patch or 'registry' in patch: plugin.sync_definitions(window, selected)
        window.statusBar().showMessage(f'Updated save settings for {len(selected)} games.', 10000)
    except (OSError, ValueError) as error:
        show_warning(window, 'Save definitions', str(error))
