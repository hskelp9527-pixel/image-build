import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json
from datetime import date, datetime, timedelta
from PIL import Image


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous = os.environ.get('IMAGE_BUILD_DATA')
        os.environ['IMAGE_BUILD_DATA'] = str(self.root / 'config')
        spec = importlib.util.spec_from_file_location('workbench_test', Path(__file__).parents[1] / 'app.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        environment = patch.object(self.module, 'environment_status', return_value={
            'installed': True, 'logged_in': True, 'image_generation': True, 'version': 'test', 'error': ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = self.module.app.test_client()
        self.folders = {kind: str(self.root / kind) for kind in self.module.KINDS}
        for path in self.folders.values():
            Path(path).mkdir()
        self.headers = {'X-Workspace-Token': self.module.TOKEN}
        self.post('settings', {'folders': self.folders})
        self.source = Path(self.folders['original']) / '中文 sample.png'
        Image.new('RGB', (400, 300), '#789abc').save(self.source)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop('IMAGE_BUILD_DATA', None)
        else:
            os.environ['IMAGE_BUILD_DATA'] = self.previous
        self.temporary.cleanup()

    def post(self, endpoint, data):
        return self.client.post('/api/' + endpoint, json=data, headers=self.headers)

    def process(self):
        return self.post('watermark', {'names': [self.source.name], 'watermark': self.module.settings['watermark']})

    def test_full_workflow_moves_queue_entries_and_preserves_dimensions(self):
        before = self.source.read_bytes()
        self.assertEqual(self.process().json['count'], 1)
        result = self.client.get('/api/state').json
        ready = result['items']['ready'][0]
        self.assertEqual(result['items']['original'], [])
        output = Path(self.folders['ready']) / ready['name']
        with Image.open(output) as image:
            self.assertEqual(image.size, (400, 300))
            self.assertNotEqual(image.convert('RGB').tobytes(), Image.open(io.BytesIO(before)).tobytes())
        self.assertFalse(self.source.exists())
        self.assertEqual(self.process().json['count'], 0)
        self.assertEqual(self.post('archive', {'names': [ready['name']]}).json['count'], 1)
        archived = self.client.get('/api/state').json['items']['archive'][0]
        self.assertEqual(self.process().json['count'], 0)
        self.assertEqual(self.post('restore', {'names': [archived['name']]}).json['count'], 1)
        self.assertEqual(len(self.module.image_paths('ready')), 1)

    def test_date_archive_and_external_file_timestamp(self):
        self.process()
        ready = self.module.image_paths('ready')[0]
        moved, errors = self.module.archive_files(automatic=True)
        self.assertEqual(moved, [])
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.module.state['ready_dates'][self.module.signature(ready)] = yesterday
        self.module.state['ready_timestamps'][self.module.signature(ready)] = datetime.combine(date.fromisoformat(yesterday),datetime.min.time()).astimezone().isoformat()
        moved, errors = self.module.archive_files(automatic=True)
        self.assertEqual(len(moved), 1)
        self.assertTrue((Path(self.folders['archive']) / yesterday / ready.name).exists())
        external = Path(self.folders['ready']) / 'external.png'
        Image.new('RGB', (40, 40)).save(external)
        older = (datetime.now() - timedelta(days=7)).timestamp()
        os.utime(external, (older, older))
        moved, errors = self.module.archive_files(automatic=True)
        self.assertEqual(len(moved), 1)
        self.assertFalse(external.exists())

    def test_midnight_archives_after_one_minute_and_timestamp_survives_rename(self):
        zone = datetime.now().astimezone().tzinfo
        before = datetime(2026, 10, 17, 23, 59, tzinfo=zone)
        after = datetime(2026, 10, 18, 0, 0, tzinfo=zone)
        with patch.object(self.module, 'local_now', return_value=before):
            self.process()
        path = self.module.image_paths('ready')[0]
        self.assertIn('__20261017_235900_', path.name)
        with Image.open(path) as image:
            self.assertEqual(datetime.fromisoformat(image.info['ImageBuildCreatedAt']), before)
        renamed = path.with_name('renamed.png')
        path.rename(renamed)
        self.module.state['ready_timestamps'].clear()
        self.module.state['ready_dates'].clear()
        with patch.object(self.module, 'local_now', return_value=after):
            moved, errors = self.module.archive_files(automatic=True)
        self.assertEqual(len(moved), 1)
        self.assertFalse(renamed.exists())
        self.assertTrue((Path(self.folders['archive'])/'2026-10-17'/'renamed.png').exists())

    def test_same_calendar_day_retained_and_restore_resets_timestamp(self):
        zone = datetime.now().astimezone().tzinfo
        start = datetime(2026, 10, 17, 0, 1, tzinfo=zone)
        later = datetime(2026, 10, 17, 23, 59, tzinfo=zone)
        with patch.object(self.module, 'local_now', return_value=start):
            self.process()
        with patch.object(self.module, 'local_now', return_value=later):
            self.assertEqual(self.module.archive_files(automatic=True)[0], [])
            self.module.archive_files()
        archived = self.module.image_paths('archive')[0].relative_to(Path(self.folders['archive'])).as_posix()
        tomorrow = datetime(2026, 10, 18, 12, 0, tzinfo=zone)
        with patch.object(self.module, 'local_now', return_value=tomorrow):
            self.assertEqual(self.post('restore', {'names':[archived]}).json['count'], 1)
            self.assertEqual(self.module.archive_files(automatic=True)[0], [])
        with Image.open(self.module.image_paths('ready')[0]) as image:
            self.assertEqual(datetime.fromisoformat(image.info['ImageBuildCreatedAt']), start)
            self.assertEqual(datetime.fromisoformat(image.info['ImageBuildReadyAt']), tomorrow)

    def test_logo_preview_and_partial_failures(self):
        image = io.BytesIO()
        Image.new('RGBA', (100, 50), (255, 0, 0, 128)).save(image, 'PNG')
        image.seek(0)
        response = self.client.post('/api/logo', data={'file': (image, 'logo.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        options = dict(self.module.settings['watermark'], mode='logo')
        response = self.post('preview', {'name': self.source.name, 'watermark': options})
        self.assertEqual(response.mimetype, 'image/png')
        corrupt = Path(self.folders['original']) / 'broken.jpg'
        corrupt.write_text('not an image')
        result = self.post('watermark', {'names': [self.source.name, corrupt.name], 'watermark': options}).json
        self.assertEqual(result['count'], 1)
        self.assertEqual(len(result['errors']), 1)

    def test_collisions_do_not_overwrite(self):
        self.process()
        ready = self.module.image_paths('ready')[0]
        target = Path(self.folders['archive']) / date.today().isoformat() / ready.name
        target.parent.mkdir()
        target.write_bytes(b'keep existing')
        self.module.archive_files()
        self.assertEqual(target.read_bytes(), b'keep existing')
        self.assertEqual(len(self.module.image_paths('archive')), 2)

    def test_folder_validation_token_and_path_escape(self):
        folders = dict(self.folders, archive=self.folders['ready'])
        self.assertEqual(self.post('settings', {'folders': folders}).status_code, 400)
        nested = Path(self.folders['ready']) / 'child'
        nested.mkdir()
        self.assertEqual(self.post('settings', {'folders': dict(self.folders, archive=str(nested))}).status_code, 400)
        self.assertEqual(self.client.post('/api/archive', json={'names': []}).status_code, 403)
        self.assertEqual(self.post('preview', {'name': '../secret.png', 'watermark': self.module.settings['watermark']}).status_code, 400)
        self.assertEqual(self.client.get('/api/state', headers={'Host': 'attacker.example'}).status_code, 403)

    def test_archive_date_uses_folder_instead_of_copied_mtime(self):
        for day in ('2026-10-15', '2026-10-16'):
            directory = Path(self.folders['archive']) / day
            directory.mkdir()
            Image.new('RGB', (40, 40)).save(directory / (day + '.png'))
        items = self.client.get('/api/state').json['items']['archive']
        self.assertEqual({item['day'] for item in items}, {'2026-10-15', '2026-10-16'})

    def test_codex_handoff_import_and_idempotency(self):
        with patch.object(self.module, 'codex_command', return_value='codex'), patch.object(self.module, 'launch_codex') as launch:
            response = self.post('codex/start', {'names': [self.source.name],
                'watermark': self.module.settings['watermark'], 'creative': '水印融入招牌', 'target': 'desktop'})
            self.assertEqual(response.status_code, 200)
            launch.assert_called_once()
        job_dir = self.module.JOBS / response.json['id']
        self.assertTrue((job_dir / 'input/001.png').exists())
        self.assertIn('水印融入招牌', (job_dir / 'prompt.txt').read_text(encoding='utf-8'))
        self.assertEqual(len(self.module.image_paths('ready')), 0)
        Image.new('RGB', (400, 300), 'red').save(job_dir / 'output/001.png')
        (job_dir / 'output/result.json').write_text(json.dumps({'outputs': [{'id': '001', 'file': '001.png'}]}))
        snapshot = self.client.get('/api/state').json
        self.assertEqual(snapshot['jobs'][0]['status'], 'completed')
        self.assertEqual(len(snapshot['items']['ready']), 1)
        self.assertEqual(snapshot['items']['original'], [])
        self.assertFalse(self.source.exists())
        self.client.get('/api/state')
        self.assertEqual(len(self.module.image_paths('ready')), 1)

    def test_codex_no_cli_and_invalid_output(self):
        data = {'names': [self.source.name], 'watermark': self.module.settings['watermark']}
        with patch.object(self.module, 'environment_status', return_value={'error':'未安装 Codex CLI'}):
            self.assertEqual(self.post('codex/start', data).status_code, 400)
        with patch.object(self.module, 'codex_command', return_value='codex'), patch.object(self.module, 'launch_codex'):
            response = self.post('codex/start', data)
        directory = self.module.JOBS / response.json['id']
        manifest = directory / 'output/result.json'
        manifest.write_text('{')
        self.assertEqual(self.client.get('/api/state').status_code, 200)
        manifest.write_text(json.dumps({'outputs':[{'id':'001','file':'../input/001.png'}]}))
        snapshot = self.client.get('/api/state').json
        self.assertEqual(len(snapshot['items']['ready']), 0)
        self.assertIn('路径无效', snapshot['jobs'][0]['error'])

    def test_failed_watermark_does_not_consume_original(self):
        before = self.source.read_bytes()
        with patch.object(self.module, 'compose', side_effect=ValueError('test failed')):
            self.assertEqual(self.process().json['count'], 0)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.module.image_paths('ready'), [])

    def test_legacy_processed_entries_are_cleaned_only_with_valid_output(self):
        key = self.module.signature(self.source)
        target = Path(self.folders['ready']) / 'existing.png'
        self.module.state['processed'][key] = str(target)
        self.client.get('/api/state')
        self.assertTrue(self.source.exists())
        Image.new('RGB',(40,40)).save(target)
        self.client.get('/api/state')
        self.assertFalse(self.source.exists())


if __name__ == '__main__':
    unittest.main()
