import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import yaml
from test_backup import APP
from ludusavi_under_test.cloud_jobs import baseline, prepare, upload
from ludusavi_under_test.plugin import BackupQueue
from PyQt6.QtWidgets import QMainWindow


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backup = self.root / 'backups'; self.folder = self.backup / 'Example'; self.folder.mkdir(parents=True)
        self.write('original')
        self.config = {'backup': {'path': str(self.backup)}, 'cloud': {'remote': 'test'}}
        config_path = self.root / 'config'; config_path.mkdir(); (config_path / 'manifest.yaml').write_text('{}')
        self.engine = SimpleNamespace(state=self.root / 'state', config=config_path, wait_for_cloud=Mock(), execute=Mock())
        self.engine.execute.return_value = {'cloud': {'save': {'change': 'Same'}}}
        self.cloud_root = baseline(self.engine, self.config, 'Example')

    def write(self, value):
        (self.folder / 'mapping.yaml').write_text(yaml.safe_dump({'name': 'Example', 'backups': [{'hash': value}]}))
        (self.folder / 'save.dat').write_text(value)

    def test_conflict_check_uses_previous_backup_and_upload_uses_immutable_new_backup(self):
        self.write('new')
        job = prepare(self.engine, self.config, 'Example', self.cloud_root)
        self.write('newer')
        def execute(args, config, **kwargs):
            root = Path(yaml.safe_load((config / 'config.yaml').read_text())['backup']['path'])
            self.assertEqual((root / 'Example/save.dat').read_text(), 'original' if '--preview' in args else 'new')
            return {'cloud': {'save': {'change': 'Same'}}}
        self.engine.execute.side_effect = execute
        self.assertEqual(upload(self.engine, job, Mock())['cloud'], 'Uploaded')
        self.assertEqual((self.cloud_root / 'baseline/Example/save.dat').read_text(), 'new')
        self.assertEqual((self.folder / 'save.dat').read_text(), 'newer')

    def test_unchanged_snapshot_skips_upload_but_checks_remote(self):
        (self.cloud_root / 'uploaded').touch()
        job = prepare(self.engine, self.config, 'Example', self.cloud_root)
        self.assertEqual(upload(self.engine, job, Mock())['cloud'], 'Up to date')
        self.assertEqual(self.engine.execute.call_count, 1)
        self.assertIn('--preview', self.engine.execute.call_args.args[0])

    def test_remote_change_prevents_upload_and_preserves_both_snapshots(self):
        self.write('new')
        job = prepare(self.engine, self.config, 'Example', self.cloud_root)
        self.engine.execute.return_value = {'cloud': {'save': {'change': 'Different'}}}
        self.assertEqual(upload(self.engine, job, Mock())['cloud'], 'Needs attention')
        self.assertEqual(self.engine.execute.call_count, 2)
        self.assertTrue(Path(job).is_dir())
        self.assertEqual((self.cloud_root / 'baseline/Example/save.dat').read_text(), 'original')

    def test_conflict_resolved_externally_adopts_matching_snapshot_without_overwrite(self):
        self.write('new')
        job = prepare(self.engine, self.config, 'Example', self.cloud_root)
        self.engine.execute.side_effect = [{'cloud': {'save': {'change': 'Different'}}},
                                           {'cloud': {'save': {'change': 'Same'}}}]
        self.assertEqual(upload(self.engine, job, Mock())['cloud'], 'Uploaded')
        self.assertTrue(all('--preview' in call.args[0] for call in self.engine.execute.call_args_list))
        self.assertEqual((self.cloud_root / 'baseline/Example/save.dat').read_text(), 'new')

    def test_remote_missing_is_reuploaded_even_when_local_unchanged(self):
        (self.cloud_root / 'uploaded').touch()
        job = prepare(self.engine, self.config, 'Example', self.cloud_root)
        self.engine.execute.return_value = {'cloud': {'save': {'change': 'New'}}}
        self.assertEqual(upload(self.engine, job, Mock())['cloud'], 'Uploaded')
        self.assertEqual(self.engine.execute.call_count, 2)

    def test_pending_uploads_coalesce_and_local_queue_is_independent(self):
        APP.processEvents()
        window = QMainWindow(); self.addCleanup(window.close)
        window.game_providers = []; window.games = [{'Id': 'a', 'Name': 'Example'}]
        owner = BackupQueue(window); tasks = []
        jobs = [prepare(self.engine, self.config, 'Example', self.cloud_root) for _ in range(3)]
        settings = {'state': str(self.engine.state)}
        result = lambda job: {'cloud_job': job, 'local_saved': True, 'cloud': 'Queued'}
        with patch('ludusavi_under_test.cloud_queue.ludusavi_gui_open', return_value=False), patch('ludusavi_under_test.plugin.ludusavi_gui_open', return_value=False), patch('ludusavi_under_test.plugin.QThreadPool.globalInstance', return_value=SimpleNamespace(start=tasks.append)):
            for job in jobs: owner.cloud_queue.add(window.games[0], settings, result(job))
            self.assertEqual(len(tasks), 1)
            self.assertFalse(Path(jobs[1]).exists())
            self.assertTrue(Path(jobs[0]).exists())
            self.assertEqual(owner.cloud_queue.pending['a'][2]['cloud_job'], jobs[2])
            owner.add({'Id': 'b', 'Name': 'Other'}, {}, settings)
            self.assertEqual(len(tasks), 2)
            self.assertEqual(owner.active[0]['Id'], 'b')

    def test_restart_resumes_only_newest_pending_snapshot(self):
        window = QMainWindow(); self.addCleanup(window.close)
        window.game_providers = []; window.games = [{'Id': 'a', 'Name': 'Example'}]
        owner = BackupQueue(window); tasks = []
        jobs = [prepare(self.engine, self.config, 'Example', self.cloud_root) for _ in range(2)]
        for job in jobs:
            path = Path(job) / 'job.json'
            meta = json.loads(path.read_text()); meta['game_id'] = 'a'; path.write_text(json.dumps(meta))
        with patch('ludusavi_under_test.cloud_queue.ludusavi_gui_open', return_value=False), patch('ludusavi_under_test.cloud_queue.QThreadPool.globalInstance', return_value=SimpleNamespace(start=tasks.append)):
            owner.cloud_queue.resume({'state': str(self.engine.state), 'cloud': True})
        self.assertEqual(len(tasks), 1)
        self.assertEqual(owner.cloud_queue.active[2]['cloud_job'], jobs[-1])
        self.assertFalse(Path(jobs[0]).exists())

    def test_old_upload_completion_cannot_overwrite_newer_local_status(self):
        window = QMainWindow(); self.addCleanup(window.close)
        owner = BackupQueue(window)
        game = {'Id': 'a', 'Name': 'Example'}
        job = prepare(self.engine, self.config, 'Example', self.cloud_root)
        owner.cloud_queue.active = (game, {}, {'cloud_job': job, 'status': 'Local backup saved'})
        owner.results['a'] = {'cloud_job': 'newer', 'cloud': 'Queued', 'status': 'Local backup saved'}
        owner.cloud_queue.completed({'cloud': 'Uploaded'})
        self.assertEqual(owner.results['a']['cloud_job'], 'newer')
        self.assertEqual(owner.results['a']['cloud'], 'Queued')
