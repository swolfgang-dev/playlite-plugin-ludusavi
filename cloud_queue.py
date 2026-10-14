"""Cloud transfers never occupy the local save-backup queue."""
import json
import shutil
import time
from pathlib import Path
from PyQt6.QtCore import QObject, QThreadPool, QTimer, pyqtSignal
from playlite.metadata_dialog import Task
from .backup import BackupEngine, ludusavi_gui_open
from .cloud_jobs import upload


class CloudQueue(QObject):
    progress = pyqtSignal(str)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.pending = {}
        self.active = None
        self.task = None
        self.attempts = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.start_next)
        self.progress.connect(self.update_progress)

    def add(self, game, settings, result):
        identity = game['Id']
        previous = self.pending.get(identity)
        if previous and previous[2]['cloud_job'] != result['cloud_job']:
            self.discard(settings, previous[2]['cloud_job'])
        self.pending[identity] = (game, settings, dict(result))
        self.start_next()

    def discard(self, settings, job):
        import fcntl
        state = Path(settings['state']); state.mkdir(parents=True, exist_ok=True)
        with (state / 'cloud.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # Another profile may be transferring this snapshot.
            shutil.rmtree(job, ignore_errors=True)

    def resume(self, settings):
        if not settings.get('cloud'):
            return
        latest = {}
        for job in sorted((Path(settings['state']) / 'cloud-snapshots').glob('*/job-*/job.json'), key=lambda p: p.stat().st_mtime):
            try:
                meta = json.loads(job.read_text())
            except (OSError, ValueError):
                continue
            if meta.get('paused') or not (job.parent / 'backup').is_dir():
                continue
            game = next((g for g in self.owner.window.games if g['Id'] == meta.get('game_id')), None)
            if game is not None:
                previous = latest.get(game['Id'])
                if previous:
                    self.discard(settings, previous[1].parent)
                latest[game['Id']] = (game, job, meta)
        for game, job, meta in latest.values():
            result = {'name': meta['name'], 'status': 'Local backup saved', 'local_saved': True,
                      'cloud': 'Queued', 'cloud_job': str(job.parent)}
            self.owner.states[game['Id']] = Path(settings['state'])
            self.owner.record_result(game, result)
            self.add(game, settings, result)

    def start_next(self):
        if self.active or not self.pending:
            return
        if ludusavi_gui_open():
            self.timer.start(3000)
            return
        identity = next(iter(self.pending))
        self.active = self.pending.pop(identity)
        game, settings, result = self.active
        def work():
            import fcntl
            engine = BackupEngine(settings)
            started = time.monotonic()
            with (engine.state / 'cloud.lock').open('a') as lock:
                self.progress.emit('Waiting for cloud synchronization…')
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not Path(result['cloud_job']).exists():
                    outcome = {'cloud': 'Superseded', 'details': 'A newer snapshot replaced this pending transfer.'}
                else:
                    outcome = upload(engine, result['cloud_job'], self.progress.emit)
            outcome['cloud_seconds'] = round(time.monotonic() - started, 3)
            return outcome
        self.task = Task(work)
        self.task.signals.succeeded.connect(self.completed)
        self.task.signals.failed.connect(lambda error: self.completed({'cloud': 'Upload failed', 'details': error}))
        QThreadPool.globalInstance().start(self.task)

    def update_progress(self, message):
        if not self.active:
            return
        game, _, result = self.active
        current = self.owner.results.get(game['Id'], {})
        if current.get('cloud_job') != result['cloud_job']:
            return
        current.update(cloud='Waiting' if message.startswith('Waiting') else 'In progress', details=message)
        self.owner.window.statusBar().showMessage(game['Name'] + ': ' + message)
        registry = getattr(self.owner.window, 'background_tasks', None)
        if registry is not None and game['Id'] in self.owner.task_ids:
            registry.update(self.owner.task_ids[game['Id']], state='Waiting' if message.startswith('Waiting') else 'Uploading' if message.startswith('Uploading') else 'Running', summary=message)

    def completed(self, outcome):
        game, settings, result = self.active
        identity = game['Id']
        completed_job = result['cloud_job']
        if outcome['cloud'] in ('Uploaded', 'Up to date', 'Superseded'):
            result.pop('cloud_job', None)
            self.attempts.pop(identity, None)
        result['cloud'] = outcome['cloud']
        result['details'] = result.get('details', '') + '\nCloud: ' + outcome.get('details', '')
        result.setdefault('timings', {}).update(cloud_seconds=outcome.get('cloud_seconds', 0))
        current = self.owner.results.get(identity, {})
        # An older transfer must never replace a newer local backup's status.
        if current.get('cloud_job') == completed_job:
            self.owner.record_result(game, result)
            self.owner.window.statusBar().showMessage(game['Name'] + ': Local backup saved · Cloud: ' + outcome['cloud'], 15000)
        if outcome['cloud'] == 'Needs attention':
            meta_path = Path(completed_job) / 'job.json'
            try:
                meta = json.loads(meta_path.read_text()); meta['paused'] = True
                meta_path.write_text(json.dumps(meta))
            except (OSError, ValueError):
                import logging
                logging.exception('Could not pause conflicting cloud snapshot')
        if outcome['cloud'] == 'Upload failed' and identity not in self.pending:
            attempts = self.attempts.get(identity, 0)
            if attempts < 3:
                self.attempts[identity] = attempts + 1
                captured = (game, settings, dict(result))
                timer = QTimer(self); timer.setSingleShot(True)
                timer.timeout.connect(lambda captured=captured, timer=timer: (self.retry(captured), timer.deleteLater()))
                timer.start(60000 * (attempts + 1))
        self.active = None
        self.task = None
        self.start_next()

    def retry(self, captured):
        game, settings, result = captured
        current = self.owner.results.get(game['Id'], {})
        if current.get('cloud_job') == result['cloud_job'] and Path(result['cloud_job']).exists():
            self.add(game, settings, result)
