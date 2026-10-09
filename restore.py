"""Preview and restore one selected backup, preserving overwritten host files."""
import json
import os
import shutil
import uuid
from pathlib import Path
from .backup import BackupEngine, vm_path


def within_roots(path, roots):
    resolved = path.resolve()
    for root in roots:
        if resolved.is_relative_to(root): return True
        # Bind-mounted home aliases and /mnt paths can name the same directory.
        for ancestor in (resolved, *resolved.parents):
            try:
                if os.path.samefile(ancestor, root): return True
            except OSError: pass
    return False


def preview_restore(settings, context, name, backup):
    engine = BackupEngine(settings)
    engine.read_paths = [context['prefix'], context['directory']]
    return engine.execute(['restore', '--preview', '--api', '--no-cloud-sync', '--backup', backup, '--', name], engine.config)


def restore_backup(settings, context, name, backup, expected):
    engine = BackupEngine(settings)
    engine.wait_for_cloud(lambda _: None)
    preview = preview_restore(settings, context, name, backup)
    if preview != expected:
        raise ValueError('Save files or the selected backup changed. Preview the restore again.')
    roots = [Path(p).resolve() for p in (context['prefix'], context['directory']) if p]
    files = [Path(p) for game in preview.get('games', {}).values() for p in game.get('files', {})]
    if any(vm_path(p) or not within_roots(p, roots) for p in files):
        raise ValueError('The backup points outside this game’s current prefix/install folder. Review its restore paths in Ludusavi.')
    recovery = engine.state / 'recovery' / uuid.uuid4().hex
    recovery.mkdir(parents=True, mode=0o700)
    originals = []
    registry = any(game.get('registry') for game in preview.get('games', {}).values())
    if registry:
        files += [Path(context['prefix']) / p for p in ('user.reg', 'system.reg', 'userdef.reg')]
    for index, path in enumerate(dict.fromkeys(files)):
        if path.is_file():
            target = recovery / str(index)
            shutil.copy2(path, target)
            originals.append({'path': str(path), 'copy': target.name})
        else:
            originals.append({'path': str(path), 'copy': None})
    (recovery / 'files.json').write_text(json.dumps(originals, indent=2))
    # Only the actual restore needs write access; scans remain read-only.
    engine.command = engine.access_command([str(p) for p in roots], writable=True)
    engine.read_paths = []
    try:
        result = engine.execute(['restore', '--force', '--api', '--no-cloud-sync', '--backup', backup, '--', name], engine.config, timeout=1200)
    except Exception as error:
        raise ValueError(f'Restore failed. Pre-restore files are preserved in {recovery}. {error}') from error
    return {'result': result, 'recovery': str(recovery)}
