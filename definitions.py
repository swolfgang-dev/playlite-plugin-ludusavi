"""Explicit Ludusavi custom definitions made from its cached manifest."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import yaml
from .backup import BackupEngine, resolve_context


def normalized(name):
    return re.sub(r'[^\w]', '', name.casefold())


def title_for(game, manifest, custom):
    override = (game.get('LudusaviBackup') or {}).get('name')
    if override:
        return override
    appid = str((game.get('MetadataIds') or {}).get('SteamMetadata') or '')
    matches = [name for name, rule in manifest.items() if appid and str((rule.get('steam') or {}).get('id')) == appid]
    if len(matches) == 1:
        return matches[0]
    matches = [name for name in set(manifest) | {entry['name'] for entry in custom}
               if normalized(name) == normalized(game.get('Name', ''))]
    return matches[0] if len(matches) == 1 else game['Name']


def read_definition(game, settings):
    engine = BackupEngine(settings)
    data = yaml.load((engine.config / 'config.yaml').read_text(), Loader=yaml.CSafeLoader) or {}
    entries = data.get('customGames') or []
    saved = game.get('LudusaviBackup') or {}
    record = engine.state / 'definitions-result.json'
    mapped = None
    if record.is_file():
        mapped = next((row['name'] for row in json.loads(record.read_text()).get('updated', []) if row.get('id') == game.get('Id')), None)
    entry = next((e for e in entries if e['name'] == (saved.get('name') or mapped)), None)
    if entry is None:
        entry = next((e for e in entries if normalized(e['name']) == normalized(game.get('Name', ''))), None)
    return copy.deepcopy(entry or {})


def sync_definitions(games, providers, settings):
    engine = BackupEngine(settings)
    config = engine.config / 'config.yaml'
    original = config.read_bytes()
    data = yaml.load(original, Loader=yaml.CSafeLoader) or {}
    manifest = yaml.load((engine.config / 'manifest.yaml').read_text(), Loader=yaml.CSafeLoader) or {}
    entries = data.setdefault('customGames', [])
    candidates = []; skipped = []
    from playlite.play_actions import actions_for
    for game in games:
        actions = [a for a in actions_for(game, providers) if a.get('Integration') in ('LutrisIntegration', 'SteamIntegration') and not a.get('IsVM')]
        contexts = []
        for action in actions:
            try:
                contexts.append((action, resolve_context(game, action, providers)))
            except ValueError:
                pass
        if not contexts:
            skipped.append(game['Name']); continue
        title = title_for(game, manifest, entries)
        rule = manifest.get(title) or {}
        old = next((e for e in entries if e['name'] == title), None)
        saved = game.get('LudusaviBackup') or {}
        files = saved.get('files', list(dict.fromkeys(list(rule.get('files', {})) + (old or {}).get('files', []))))
        registry = saved.get('registry', list(dict.fromkeys(list(rule.get('registry', {})) + (old or {}).get('registry', []))))
        if not files and not registry and 'files' not in saved:
            skipped.append(game['Name']); continue
        entry = copy.deepcopy(old or {'name': title})
        entry.update(name=title, integration='override', files=files, registry=registry)
        prefixes = entry.setdefault('winePrefix', [])
        for action, context in contexts:
            prefix = str(action.get('Prefix') or context['prefix'])
            if prefix not in prefixes:
                prefixes.append(prefix)
        folders = entry.setdefault('installDir', [])
        for _, context in contexts:
            name = Path(context['directory']).name if context['directory'] else ''
            if name and name not in folders:
                folders.append(name)
        if old is not None:
            entries[entries.index(old)] = entry
        else:
            entries.append(entry)
        candidates.append((game, title, entry, old))
    engine.state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='definitions-', dir=engine.state) as temp:
        stage = Path(temp)
        (stage / 'config.yaml').write_text(yaml.safe_dump(data, sort_keys=False))
        shutil.copy2(engine.config / 'manifest.yaml', stage / 'manifest.yaml')
        names = list(dict.fromkeys(title for _, title, _, _ in candidates))
        preview = engine.execute(['backup', '--preview', '--api', '--no-cloud-sync', '--', *names], stage, timeout=180) if names else {}
        updated = []
        for game, title, entry, old in candidates:
            files = (preview.get('games', {}).get(title) or {}).get('files', {})
            useful = [p for p in files if not p.endswith(('.reg', '.log', '_log.txt')) and '/Unity/' not in p]
            if not useful and not old:
                entries.remove(entry); skipped.append(game['Name']); continue
            updated.append({'id': game['Id'], 'game': game['Name'], 'name': title, 'files': len(files)})
        if config.read_bytes() != original:
            raise ValueError('Ludusavi settings changed during synchronization. Retry after it finishes.')
        backup = engine.state / ('config-before-definitions-' + uuid.uuid4().hex + '.yaml')
        shutil.copy2(config, backup)
        temporary = config.with_name(config.name + '.playlite.tmp')
        try:
            temporary.write_text(yaml.safe_dump(data, sort_keys=False))
            temporary.chmod(config.stat().st_mode & 0o777)
            temporary.replace(config)
        finally:
            temporary.unlink(missing_ok=True)
    result = {'updated': updated, 'skipped': skipped, 'config_backup': str(backup)}
    record = engine.state / 'definitions-result.json'
    previous = json.loads(record.read_text()).get('updated', []) if record.exists() else []
    identities = {row['id'] for row in updated}
    record.write_text(json.dumps({**result, 'updated': [row for row in previous if row['id'] not in identities] + updated}, indent=2))
    return result
