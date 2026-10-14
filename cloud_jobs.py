"""Immutable per-game snapshots for independent, recoverable cloud uploads."""
import hashlib
import fcntl
import json
import shutil
import subprocess
import uuid
from pathlib import Path
import yaml


def game_folder(root, name):
    for mapping in Path(root).glob('*/mapping.yaml'):
        if (yaml.load(mapping.read_bytes(), Loader=yaml.CSafeLoader) or {}).get('name') == name:
            return mapping.parent
    return None


def clone_game(source, destination, name):
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = game_folder(source, name)
    if folder is not None:
        # Reflinks avoid copying save payloads twice on supported filesystems.
        subprocess.run(['cp', '-a', '--reflink=auto', '--', str(folder), str(destination)], check=True, timeout=1200)


def identity(config, name):
    return hashlib.sha256(json.dumps([config['backup']['path'], config.get('cloud'), name], sort_keys=True).encode()).hexdigest()


def baseline(engine, config, name):
    root = engine.state / 'cloud-snapshots' / identity(config, name)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    original = root / 'baseline'
    with (root / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not original.exists() and list(root.glob('previous-*')):
            max(root.glob('previous-*'), key=lambda p: p.stat().st_mtime_ns).rename(original)
        if not original.exists():
            staging = root / ('baseline-' + uuid.uuid4().hex)
            clone_game(Path(config['backup']['path']).expanduser(), staging, name)
            staging.rename(original)
    return root


def prepare(engine, config, name, root, game_id=None):
    if game_folder(Path(config['backup']['path']).expanduser(), name) is None:
        raise ValueError('The game backup folder could not be located; cloud upload was not queued.')
    job = root / ('job-' + uuid.uuid4().hex)
    job.mkdir(mode=0o700)
    clone_game(Path(config['backup']['path']).expanduser(), job / 'backup', name)
    copied = dict(config, backup=dict(config['backup'], path=str(job / 'backup')))
    (job / 'config.yaml').write_text(yaml.safe_dump(copied))
    (job / 'config.yaml').chmod(0o600)
    shutil.copy2(engine.config / 'manifest.yaml', job / 'manifest.yaml')
    (job / 'job.json').write_text(json.dumps({'name': name, 'root': str(root), 'game_id': game_id}))
    return str(job)


def same_snapshot(first, second, name):
    a, b = game_folder(first, name), game_folder(second, name)
    if a is None or b is None:
        return a is b
    # mapping.yaml records hashes of every file and all retained backups.
    return (a / 'mapping.yaml').read_bytes() == (b / 'mapping.yaml').read_bytes()


def upload(engine, job, status):
    job = Path(job)
    meta = json.loads((job / 'job.json').read_text())
    name, root = meta['name'], Path(meta['root'])
    if not (job / 'backup').is_dir():
        raise ValueError('Cloud snapshot is incomplete; create a new local backup before retrying.')
    config = yaml.load((job / 'config.yaml').read_bytes(), Loader=yaml.CSafeLoader)
    with (root / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (root / 'baseline').exists():
            previous = list(root.glob('previous-*'))
            if not previous:
                raise ValueError('The previous cloud comparison snapshot is unavailable; review cloud backups in Ludusavi.')
            max(previous, key=lambda p: p.stat().st_mtime_ns).rename(root / 'baseline')
    engine.read_paths = [str(root)]
    engine.wait_for_cloud(status)
    already_uploaded = False
    import tempfile
    with tempfile.TemporaryDirectory(dir=root, prefix='check-') as temp:
        check = Path(temp)
        config['backup']['path'] = str(root / 'baseline')
        (check / 'config.yaml').write_text(yaml.safe_dump(config))
        (check / 'config.yaml').chmod(0o600)
        shutil.copy2(job / 'manifest.yaml', check / 'manifest.yaml')
        status('Checking cloud for conflicts…')
        before = engine.execute(['cloud', 'upload', '--preview', '--api', '--', name], check, timeout=60)
        if any(item['change'] not in ('New', 'Same') for item in before.get('cloud', {}).values()):
            # A conflict may already have been resolved through Ludusavi.
            current = engine.execute(['cloud', 'upload', '--preview', '--api', '--', name], job, timeout=60)
            already_uploaded = all(item['change'] == 'Same' for item in current.get('cloud', {}).values())
            if not already_uploaded:
                return {'cloud': 'Needs attention', 'details': 'Cloud conflict: open Ludusavi to choose which copy to keep.'}
    marker = root / 'uploaded'
    if marker.exists() and all(item['change'] == 'Same' for item in before.get('cloud', {}).values()) and same_snapshot(root / 'baseline', job / 'backup', name):
        shutil.rmtree(job)
        return {'cloud': 'Up to date', 'details': 'Backup matches the last uploaded snapshot.'}
    if not already_uploaded:
        status('Uploading save backup…')
        engine.execute(['cloud', 'upload', '--force', '--api', '--', name], job, timeout=None)
    # The immutable uploaded snapshot becomes the next comparison baseline.
    with (root / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = root / ('previous-' + uuid.uuid4().hex)
        (root / 'baseline').rename(previous)
        try:
            (job / 'backup').rename(root / 'baseline')
        except BaseException:
            previous.rename(root / 'baseline')
            raise
        marker.touch()
        shutil.rmtree(previous)
    shutil.rmtree(job)
    return {'cloud': 'Uploaded', 'details': 'Cloud upload complete.'}
