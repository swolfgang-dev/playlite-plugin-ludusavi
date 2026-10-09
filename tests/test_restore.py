import test_backup
import unittest
import json
from pathlib import Path
from unittest.mock import patch
from ludusavi_under_test.restore import restore_backup


class RestoreTests(unittest.TestCase):
    setUp=test_backup.BackupTests.setUp

    def test_restore_preserves_current_file_before_writing(self):
        save=self.prefix/'drive_c/save.dat';save.write_bytes(b'current')
        preview={'games':{'Example':{'files':{str(save):{'change':'Different','bytes':3}}}}}
        calls=[]
        def execute(engine,args,config,timeout=180):
            calls.append(args)
            if '--preview' in args:return preview
            save.write_bytes(b'old');return {'games':{'Example':{}}}
        with patch('ludusavi_under_test.restore.BackupEngine.execute',execute):
            result=restore_backup(self.engine.settings,self.context,'Example','selected',preview)
        folder=Path(result['recovery']);mapping=json.loads((folder/'files.json').read_text())
        self.assertEqual((folder/mapping[0]['copy']).read_bytes(),b'current')
        self.assertEqual(save.read_bytes(),b'old')
        self.assertIn('selected',calls[-1]);self.assertIn('--no-cloud-sync',calls[-1])

    def test_changed_preview_prevents_restore(self):
        with patch('ludusavi_under_test.restore.BackupEngine.execute',return_value={'games':{}}):
            with self.assertRaisesRegex(ValueError,'changed'):
                restore_backup(self.engine.settings,self.context,'Example','selected',{'before':True})
        self.assertFalse((Path(self.engine.settings['state'])/'recovery').exists())

    def test_outside_prefix_is_not_overwritten(self):
        preview={'games':{'Example':{'files':{'/tmp/unrelated':{}}}}}
        with patch('ludusavi_under_test.restore.BackupEngine.execute',return_value=preview):
            with self.assertRaisesRegex(ValueError,'outside'):
                restore_backup(self.engine.settings,self.context,'Example','selected',preview)
