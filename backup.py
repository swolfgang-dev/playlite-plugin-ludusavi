"""Ludusavi CLI integration with isolated host roots and conservative cloud sync."""
import copy
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import time
import yaml

APP_ID = 'com.github.mtkennerly.ludusavi'


def cloud_busy(proc=Path('/proc')):
    """Find this user's Ludusavi cloud commands and its rclone transfers."""
    processes = {}
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            args = (path / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            name = (path / 'comm').read_text().strip().lower()
            parent = next(line.split()[1] for line in (path / 'status').read_text().splitlines()
                          if line.startswith('PPid:'))
            processes[path.name] = (name, args, parent)
        except (OSError, StopIteration):
            continue  # A process may finish while being inspected.
    for pid, (name, args, parent) in processes.items():
        if name == 'ludusavi' and 'cloud' in args:
            return True
        if name != 'rclone' or not any(command in args for command in ('copy', 'copyto', 'sync', 'move', 'moveto')):
            continue
        visited = {pid}
        while parent in processes and parent not in visited:
            visited.add(parent)
            ancestor, _, parent = processes[parent]
            if ancestor == 'ludusavi':
                return True
    return False


def host_environment():
    env = dict(os.environ)
    if env.get('PLAYLITE_PROFILE') == 'repo':
        env['HOME'] = env.get('PLAYLITE_HOST_HOME') or pwd.getpwuid(os.getuid()).pw_dir
        for kind in ('DATA', 'CONFIG', 'CACHE', 'STATE'):
            key = f'XDG_{kind}_HOME'
            original = env.get('PLAYLITE_HOST_' + key)
            if original:
                env[key] = original
            else:
                env.pop(key, None)
    return env


def vm_path(value):
    return any(part.lower() in {'vm', 'vms', 'steam-vm', 'virtualmachines', 'virtual machines'}
               for part in Path(value).parts)


def resolve_context(game, action, providers):
    override = game.get('LudusaviBackup') or {}
    if not action:
        if not override.get('prefix'):
            raise ValueError('Choose a host Steam/Lutris play action or configure a host Wine prefix in Edit → Installation.')
        action = {'Integration': 'ConfiguredHost'}
    identity = action.get('Integration')
    if identity not in ('LutrisIntegration', 'SteamIntegration', 'ConfiguredHost') or action.get('IsVM') or str(action.get('Environment', '')).lower() in ('vm', 'virtual'):
        raise ValueError('Automatic save backups support host Steam and Lutris actions. VM actions are excluded.')
    prefix = override.get('prefix') or action.get('Prefix') or ''
    directory = action.get('InstallDirectory') or game.get('InstallDirectory') or ''
    if not prefix and identity == 'LutrisIntegration':
        provider = next((p for p in providers if p.id == identity), None)
        if provider is None:
            raise ValueError('The Lutris integration is unavailable.')
        config = provider.launch_configuration(action['GameId'])
        prefix = (config.get('game') or {}).get('prefix') or ''
    if not prefix and identity == 'SteamIntegration':
        env = host_environment()
        home = Path(env['HOME'])
        roots = [home / '.steam/steam', home / '.steam/debian-installation',
                 Path(env.get('XDG_DATA_HOME', str(home / '.local/share'))) / 'Steam',
                 home / '.var/app/com.valvesoftware.Steam/data/Steam']
        if directory:
            path = Path(directory)
            if path.parent.name == 'common' and path.parent.parent.name == 'steamapps':
                roots.insert(0, path.parent.parent.parent)
        prefixes = {str((root / 'steamapps/compatdata' / str(action['GameId']) / 'pfx').resolve())
                    for root in roots if (root / 'steamapps/compatdata' / str(action['GameId']) / 'pfx/drive_c').is_dir()}
        if len(prefixes) > 1:
            raise ValueError('Multiple Proton prefixes found. Set the host prefix explicitly in Edit → Installation.')
        prefix = next(iter(prefixes), '')
    if not prefix:
        raise ValueError('No host Wine/Proton prefix found. Configure it in Edit → Installation before backing up.')
    path = Path(prefix).expanduser()
    if not path.is_absolute() or not (path / 'drive_c').is_dir() or vm_path(path) or vm_path(path.resolve()):
        raise ValueError('Use an existing host Wine/Proton prefix containing drive_c. VM paths are excluded.')
    if directory and (vm_path(directory) or vm_path(Path(directory).resolve())):
        raise ValueError('VM installation paths are excluded from save backups.')
    return {'prefix': str(path.resolve()), 'directory': directory,
            'name': override.get('name') or game.get('Name') or '',
            'steam_id': (game.get('MetadataIds') or {}).get('SteamMetadata') or
                        (action.get('GameId') if identity == 'SteamIntegration' else None),
            'exact_name': bool(override.get('name')),
            **{key: override[key] for key in ('files', 'registry') if key in override}}


class BackupEngine:
    def __init__(self, settings):
        self.settings = settings
        self.env = host_environment()
        home = Path(self.env['HOME'])
        native = settings.get('native', '')
        self.command = [native] if native else ['flatpak', 'run', APP_ID]
        self.config = Path(settings.get('config') or (home / '.config/ludusavi' if native else
            home / '.var/app' / APP_ID / 'config/ludusavi'))
        self.state = Path(settings['state'])

    def execute(self, args, config, timeout=180):
        try:
            completed = subprocess.run(self.command + ['--config', str(config), '--no-manifest-update'] + args,
                env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise ValueError('Ludusavi timed out. Retry later or check the cloud status in Ludusavi.') from error
        try:
            payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        except json.JSONDecodeError as error:
            raise ValueError('Ludusavi returned an invalid response.') from error
        errors = payload.get('errors') or {}
        if completed.returncode or any(value is not None and value is not False for value in errors.values()):
            raise ValueError((completed.stderr.strip() or json.dumps(payload.get('errors')) or 'Ludusavi failed.')[-2500:])
        return payload

    def wait_for_cloud(self, status):
        waiting = False
        while cloud_busy():
            if not waiting:
                status('Waiting for the current Ludusavi upload to finish…')
                waiting = True
            time.sleep(1)
        if waiting:
            status('Backing up saves…')

    def run(self, context, preview=False, status=lambda message: None):
        if not (self.config / 'config.yaml').is_file() or not (self.config / 'manifest.yaml').is_file():
            raise ValueError('Open Ludusavi and run Preview once to create its configuration and game manifest.')
        config = yaml.safe_load((self.config / 'config.yaml').read_text()) or {}
        config = copy.deepcopy(config)
        # Never scan VM drives or other configured launcher libraries for this job.
        config['roots'] = [{'store': 'otherWine', 'path': context['prefix']}]
        if context['directory']:
            config['roots'].append({'store': 'other', 'path': str(Path(context['directory']).parent)})
        config['redirects'] = []
        if 'files' in context:
            entries = config.setdefault('customGames', [])
            entry = next((e for e in entries if e['name'] == context['name']), None)
            if entry is None:
                entry = {'name': context['name']}
                entries.append(entry)
            entry.update(integration='override', files=context['files'],
                         registry=context.get('registry', []), winePrefix=[context['prefix']])
        self.state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='scan-', dir=self.state) as temp:
            isolated = Path(temp)
            (isolated / 'config.yaml').write_text(yaml.safe_dump(config))
            shutil.copy2(self.config / 'manifest.yaml', isolated / 'manifest.yaml')
            args = ['find', '--backup', '--api']
            if context['steam_id'] and not context['exact_name']:
                args += ['--steam-id', str(context['steam_id'])]
            if not context['exact_name']:
                args += ['--normalized']
            found = self.execute(args + ['--', context['name']], isolated)
            matches = list(found.get('games', {}))
            if len(matches) != 1:
                raise ValueError('No unique Ludusavi match. Set the Ludusavi title in Edit → Installation or create a Custom Game in Ludusavi.')
            name = matches[0]
            sync = bool(self.settings.get('cloud', True) and (config.get('cloud') or {}).get('synchronize') and (config.get('cloud') or {}).get('remote'))
            before, cloud_problem = None, None
            if sync and not preview:
                self.wait_for_cloud(status)
                # Verify the previous backup matches the cloud before making local changes.
                try:
                    before = self.execute(['cloud', 'upload', '--preview', '--api', '--', name], isolated, timeout=60)
                    if any(item['change'] not in ('New', 'Same') for item in before.get('cloud', {}).values()):
                        cloud_problem = 'Cloud conflict: open Ludusavi to choose which copy to keep.'
                except ValueError as error:
                    cloud_problem = str(error)
            local_args = ['backup', '--api', '--force', '--no-cloud-sync', '--wine-prefix', context['prefix']]
            scan = self.execute(local_args + ['--preview', '--', name], isolated)
            files = [path for result in scan.get('games', {}).values() for path in result.get('files', {})]
            if any(vm_path(path) or vm_path(Path(path).resolve()) for path in files):
                raise ValueError('The preview includes a VM save path. Exclude that path in Ludusavi before backing up.')
            if not files:
                return {'name': name, 'status': 'No saves found', 'local_saved': False, 'cloud': 'Not attempted', 'details': 'Check the Ludusavi title, prefix, and custom save locations.'}
            if preview:
                return {'name': name, 'status': 'Preview', 'files': files, 'local_saved': False, 'cloud': 'Not attempted'}
            result = self.execute(local_args + ['--', name], isolated, timeout=1200)
            saved = {'name': name, 'status': 'Local backup saved', 'local_saved': True,
                     'cloud': 'Disabled', 'details': json.dumps(result, indent=2)}
            if sync:
                if cloud_problem:
                    saved.update(cloud='Needs attention', details=cloud_problem)
                else:
                    try:
                        self.wait_for_cloud(status)
                        status('Uploading save backup…')
                        self.execute(['cloud', 'upload', '--force', '--api', '--', name], isolated, timeout=None)
                        saved.update(status='Backup complete', cloud='Uploaded')
                    except ValueError as error:
                        saved.update(cloud='Upload failed', details=str(error))
            return saved
