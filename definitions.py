"""Explicit Ludusavi custom definitions made from its cached manifest."""
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import yaml
from .backup import BackupEngine, resolve_context, emulator_save_paths, gse_save_paths


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


def definition_fingerprint(games, settings):
    """Hash definition inputs, excluding Ludusavi UI and backup runtime state."""
    import hashlib
    engine = BackupEngine(settings)
    config = yaml.load((engine.config / 'config.yaml').read_bytes(), Loader=yaml.CSafeLoader) or {}
    manifest = hashlib.sha256()
    with (engine.config / 'manifest.yaml').open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            manifest.update(chunk)
    ownership = engine.state / 'managed-definitions.json'
    inputs = {
        'schema': 2,
        'config': str(engine.config.resolve()),
        'manifest': manifest.hexdigest(),
        'games': [{key: game.get(key) for key in
                   ('Id', 'Name', 'PlayActions', 'InstallDirectory', 'MetadataIds', 'LudusaviBackup')}
                  for game in games],
        'definitions': {key: config.get(key) for key in ('customGames', 'roots', 'redirects')},
        'managed': json.loads(ownership.read_text()) if ownership.exists() else {},
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


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


def sync_definitions(games, providers, settings, reconcile=False):
    engine = BackupEngine(settings)
    config = engine.config / 'config.yaml'
    original = config.read_bytes()
    data = yaml.load(original, Loader=yaml.CSafeLoader) or {}
    manifest = yaml.load((engine.config / 'manifest.yaml').read_text(), Loader=yaml.CSafeLoader) or {}
    entries = data.setdefault('customGames', [])
    engine.state.mkdir(parents=True, exist_ok=True)
    ownership = engine.state / 'managed-definitions.json'
    managed = json.loads(ownership.read_text()) if ownership.exists() else {}
    if reconcile:
        known = {g['Id'] for g in games}
        removed = [key for key in managed if key not in known]
        keep = {row['name'] for key, row in managed.items() if key in known}
        for key in removed:
            name = managed.pop(key)['name']
            if name not in keep:
                entries[:] = [e for e in entries if e['name'] != name]
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
        previous = managed.get(game['Id'], {})
        inferred = list(dict.fromkeys(p for _, c in contexts for p in emulator_save_paths(c) + gse_save_paths(c)))
        generated_files = list(dict.fromkeys(list(rule.get('files', {})) + inferred))
        generated_registry = list(rule.get('registry', {}))
        extras = [p for p in (old or {}).get('files', []) if p not in previous.get('generated_files', [])]
        registry_extras = [p for p in (old or {}).get('registry', []) if p not in previous.get('generated_registry', [])]
        files = list(dict.fromkeys(saved.get('files', generated_files + extras) + inferred))
        registry = saved.get('registry', list(dict.fromkeys(generated_registry + registry_extras)))
        if not files and not registry and 'files' not in saved:
            skipped.append(game['Name']); continue
        entry = copy.deepcopy(old or {'name': title})
        entry.update(name=title, integration='override', files=files, registry=registry)
        prefixes = []; entry['winePrefix'] = prefixes
        for action, context in contexts:
            prefix = str(action.get('Prefix') or context['prefix'])
            if prefix not in prefixes:
                prefixes.append(prefix)
        folders = []; entry['installDir'] = folders
        for _, context in contexts:
            name = Path(context['directory']).name if context['directory'] else ''
            if name and name not in folders:
                folders.append(name)
        if old is not None:
            entries[entries.index(old)] = entry
        else:
            entries.append(entry)
        old_title = previous.get('name')
        if old_title and old_title != title and not any(row.get('name') == old_title for key, row in managed.items() if key != game['Id']):
            entries[:] = [e for e in entries if e['name'] != old_title]
        managed[game['Id']] = {'name': title, 'generated_files': generated_files, 'generated_registry': generated_registry}
        candidates.append((game, title, entry, old))
    engine.state.mkdir(parents=True, exist_ok=True)
    roots=data.setdefault('roots', [])
    for entry in entries:
        if entry['name'] in {row['name'] for row in managed.values()}:
            for prefix in entry.get('winePrefix', []):
                root={'store':'otherWine','path':prefix}
                if root not in roots: roots.append(root)
    updated = [{'id': game['Id'], 'game': game['Name'], 'name': title,
                'file_rules': len(entry['files'])} for game, title, entry, old in candidates]
    backup = None
    if yaml.load(original, Loader=yaml.CSafeLoader) != data:
        backup = engine.state / ('config-before-definitions-' + uuid.uuid4().hex + '.yaml')
        backup.write_bytes(original); backup.chmod(0o600)
        descriptor, filename = tempfile.mkstemp(prefix='config.playlite-', dir=config.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(yaml.safe_dump(data, sort_keys=False))
            temporary.chmod(config.stat().st_mode & 0o777)
            if config.read_bytes() != original:
                raise ValueError('Ludusavi settings changed during synchronization. Retry after it finishes.')
            temporary.replace(config)
        finally:
            temporary.unlink(missing_ok=True)
    result = {'updated': updated, 'skipped': skipped, 'config_backup': str(backup) if backup else None}
    record = engine.state / 'definitions-result.json'
    previous = json.loads(record.read_text()).get('updated', []) if record.exists() else []
    identities = {row['id'] for row in updated}
    retained = [row for row in previous if row['id'] not in identities and (not reconcile or row['id'] in {g['Id'] for g in games})]
    record.write_text(json.dumps({**result, 'updated': retained + updated}, indent=2))
    ownership.write_text(json.dumps(managed, indent=2))
    engine.grant_persistent_access([p for entry in entries if entry['name'] in {row['name'] for row in managed.values()} for p in entry.get('winePrefix', [])])
    return result


def register_detected_locations(game, contexts, settings):
    captured = copy.deepcopy(game)
    captured['PlayActions'] = [{'Integration': 'LutrisIntegration', 'Prefix': c['prefix'],
                                'InstallDirectory': c['directory']} for c in contexts]
    result = sync_definitions([captured], [], settings)
    return next((row['name'] for row in result['updated']), game['Name'])


def rule_sources(game, definition, settings, context):
    """Explain saved rules using the same manifest and emulator readers as sync."""
    engine = BackupEngine(settings)
    try:
        manifest = yaml.load((engine.config / 'manifest.yaml').read_text(), Loader=yaml.CSafeLoader) or {}
    except (OSError, ValueError, yaml.YAMLError):
        manifest = {}
    title = definition.get('name') or title_for(game, manifest, [])
    base = manifest.get(title) or {}
    codex = set(emulator_save_paths(context))
    goldberg = set(gse_save_paths(context))
    overrides = game.get('LudusaviBackup') or {}
    result = []
    for kind in ('files', 'registry'):
        for rule in definition.get(kind, []):
            if kind == 'files' and rule in codex:
                source = 'CODEX configuration'
            elif kind == 'files' and rule in goldberg:
                source = 'Goldberg save directory'
            elif rule in overrides.get(kind, []):
                source = 'Manual override'
            elif rule in (base.get(kind) or {}):
                source = 'Ludusavi manifest'
            else:
                source = 'Custom definition'
            result.append((kind, rule, source))
    return result
